import sys
import threading
import time
import signal
import yaml
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from itService import ItServiceHandler
from logHelper import app_base_dir, logger, reconfigure_logging_from_cfg
from melsec import (
    MelsecHandler,
    max_connections_from_config,
    normalize_plc_configs,
)
from web import WEB_BIND_HOST, WEB_BIND_PORT, WebAPI
from licenseHelp import (
    PRODUCT_ID,
    LicenseHelper,
    existing_license_crt_path,
    load_license_binding_meta,
)

TRIAL_SECONDS = 3600

def resolve_config_path() -> Path:
    primary = Path("/config.yaml")
    if primary.exists():
        return primary
    if getattr(sys, "frozen", False):
        return app_base_dir() / "config.yaml"
    return Path("config.yaml")

class Controller:
    """PLC 輪詢 + Influx/Mongo；Web 固定埠，軟重載不結束行程。"""

    def __init__(self) -> None:
        self.config_path = resolve_config_path()
        self.cfg = self._loadConfig()
        self.web: WebAPI | None = None
        self.melsec_handlers: list[MelsecHandler | None] = []
        self.itServiceHandler = None
        self.executor: ThreadPoolExecutor | None = None
        self._poll_stop = threading.Event()
        self._plc_threads: list[threading.Thread] = []
        self._reload_lock = threading.Lock()
        self._shutdown = threading.Event()
        self.license_status = False
        self.start_time = None
        self.running_status = "starting"
        self.remain_time = TRIAL_SECONDS

    def _loadConfig(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(
                "Config file not found: /config.yaml, ./config.yaml (dev), "
                "or config.yaml next to the executable (frozen)"
            )
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def reload_config_from_disk(self) -> None:
        self.cfg = self._loadConfig()

    def stop_tags_loop(self) -> None:
        """停止輪詢執行緒並關閉 thread pool。"""
        self._poll_stop.set()
        threads = list(self._plc_threads)
        for t in threads:
            if t.is_alive():
                t.join(timeout=60.0)
                if t.is_alive():
                    logger.warning("PLC 輪詢執行緒 %s 在逾時後仍未結束", t.name)
        self._plc_threads = []
        self._poll_stop.clear()

        ex = self.executor
        if ex is not None:
            try:
                ex.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=True)
            self.executor = None

    def disconnect_data_services(self) -> None:
        """關閉 Influx/Mongo 與 PLC 連線。"""
        if self.itServiceHandler is not None:
            try:
                self.itServiceHandler.close()
            except Exception as e:
                logger.warning("關閉 ItService 時例外: %s", e)
            self.itServiceHandler = None

        for h in self.melsec_handlers:
            if h is None:
                continue
            try:
                h.close()
            except Exception as e:
                logger.warning("關閉 Melsec 時例外: %s", e)
        self.melsec_handlers = []

    def reconnect_data_services(self) -> None:
        """依目前 self.cfg 建立新的 ItServiceHandler。"""
        self.itServiceHandler = ItServiceHandler(self.cfg)

    def _initMelsecHandlers(self) -> None:
        self.melsec_handlers = []
        for plc_cfg in normalize_plc_configs(self.cfg):
            name = plc_cfg.get("name", "Unknown")
            try:
                handler = MelsecHandler(name, plc_cfg)
                self.melsec_handlers.append(handler)
                n = handler.max_connections
                logger.info(
                    "Initialized MELSEC PLC: %s at %s (%s connection session%s)",
                    name,
                    plc_cfg.get("ip"),
                    n,
                    "s" if n != 1 else "",
                )
            except Exception as e:
                logger.error("Failed to initialize PLC %s: %s", name, e)
                self.melsec_handlers.append(None)

    def start_tags_loop(self) -> None:
        """依每台 PLC 的 tags 啟動各自輪詢執行緒（互不影響）。"""
        if any(t.is_alive() for t in self._plc_threads):
            logger.warning("PLC 輪詢已在執行中，略過重複啟動")
            return
        if self.executor is not None:
            logger.warning("executor 仍存在，請先 stop_tags_loop")
            return

        plc_cfgs = normalize_plc_configs(self.cfg)
        if not plc_cfgs:
            logger.error("設定中找不到 PLC（melsec 或 melsecs）")
            return

        if self.melsec_handlers:
            pool_size = sum(
                (h.max_connections if h is not None else 0)
                for h in self.melsec_handlers
            )
        else:
            pool_size = sum(max_connections_from_config(p) for p in plc_cfgs)
        pool_size = max(1, pool_size)
        self.executor = ThreadPoolExecutor(max_workers=pool_size)
        self._poll_stop.clear()

        self._plc_threads = []
        for idx, plc_cfg in enumerate(plc_cfgs):
            handler = self.melsec_handlers[idx] if idx < len(self.melsec_handlers) else None
            name = plc_cfg.get("name", f"PLC_{idx+1}")
            t = threading.Thread(
                target=self._run_single_plc_loop,
                args=(plc_cfg, handler),
                name=f"plc-loop-{name}",
                daemon=True,
            )
            self._plc_threads.append(t)
            t.start()

        logger.info(
            "PLC polling threads started (%s PLC, pool size = %s).",
            len(self._plc_threads),
            pool_size,
        )

    def soft_reload_runtime(self) -> None:
        """不結束行程：停輪詢 → 斷線 → 重讀設定 → 重連 DB/PLC → 再啟輪詢。"""
        with self._reload_lock:
            self.start_time = time.time()
            self.license_status = self.check_license()
            self._tick_license()
            if self.license_status:
                logger.info("重載後：正式授權（無試用限制）")
            else:
                logger.info(
                    "重載後：試用模式，剩餘約 %s 秒",
                    self.remain_time,
                )
            logger.info("開始重載（Web 保持 %s:%s）…", WEB_BIND_HOST, WEB_BIND_PORT)
            self.running_status = "reloading"
            try:
                self.stop_tags_loop()
                self.disconnect_data_services()
                self.reload_config_from_disk()
                reconfigure_logging_from_cfg(self.cfg)
                self.reconnect_data_services()
                self._initMelsecHandlers()
                self.start_tags_loop()
                self.running_status = "running"
                logger.info("重載完成")
            except Exception as e:
                self.running_status = "error"
                logger.critical("重載失敗: %s", e)
                raise

    def _run_single_plc_loop(self, plc_cfg: dict, handler: MelsecHandler | None) -> None:
        """單台 PLC 輪詢迴圈：連線/timeout 只影響本 PLC，不拖慢其他 PLC。"""
        name = plc_cfg.get("name", "Unknown")
        try:
            poll_interval = float(plc_cfg.get("poll_interval", 3.0))
        except (TypeError, ValueError):
            poll_interval = 3.0

        while not self._poll_stop.is_set():
            start_time = time.time()
            plc_payload: dict = {"name": name}

            ex = self.executor
            if ex is None:
                break

            tags = plc_cfg.get("tags", {}) or {}
            futures = []
            for tag_name, details in tags.items():
                if self._poll_stop.is_set():
                    break
                futures.append(
                    ex.submit(
                        self.poll_tag,
                        handler,
                        tag_name,
                        details,
                        plc_payload,
                    )
                )

            for f in futures:
                if self._poll_stop.is_set():
                    break
                try:
                    f.result()
                except Exception as e:
                    logger.error("PLC [%s] tag 任務例外: %s", name, e)

            if self._poll_stop.is_set():
                break

            if self.itServiceHandler and len(plc_payload) > 1:
                try:
                    self.itServiceHandler.insertMessageToInfluxDB(plc_payload)
                    self.itServiceHandler.insertMessageToMongoDB(plc_payload)
                except Exception as e:
                    logger.error("PLC [%s] 資料後端寫入例外（略過本輪）: %s", name, e)

            elapsed = time.time() - start_time
            wait = max(0.0, poll_interval - elapsed)
            end = time.time() + wait
            while time.time() < end and not self._poll_stop.is_set():
                time.sleep(0.05)

        logger.info("PLC [%s] 輪詢迴圈已結束（stop_tags_loop）", name)

    def poll_tag(
        self,
        handler: MelsecHandler | None,
        tag_name: str,
        details: dict,
        result_dict: dict,
    ) -> None:
        if handler is None:
            return
        try:
            val = handler.read(tag_name, details)
            if val is not None:
                result_dict[tag_name] = val
        except Exception as e:
            logger.error("Poll error on %s: %s", tag_name, e)
    
    def check_license(self):
        try:
            crt = existing_license_crt_path()
            if crt is None:
                logger.info("license.crt not found (artifact dir or exe dir)")
                return False

            cert_bytes = crt.read_bytes()

            meta = load_license_binding_meta()
            if meta is None:
                logger.info(
                    "license binding not found (need CSR generation: license.binding or legacy license_meta.json)"
                )
                return False

            licMgr = LicenseHelper()
            
            # 確保 verifyLicense 能處理 bytes 格式的憑證
            verifyResult = licMgr.verifyLicense(
                licenseCert=cert_bytes, 
                encryptedAESKey=meta["encrypedAESKey"], 
                iv_hex=meta["iv_hex"]
            )

            if verifyResult.get("status"):
                if verifyResult.get("productId") == PRODUCT_ID:
                    logger.info(f"Licensed for {PRODUCT_ID}")
                    return True
                else:
                    logger.info("license invalid on product ID")
                    return False
            else:
                logger.info("license invalid")
                return False
        except Exception as error:
            logger.error(f"Error on check_license, {error}")
            return False
    
    def _tick_license(self):
        """Calculates remaining time and triggers stop if expired."""
        if self.license_status:
            self.remain_time = -1
        else:
            elapsed = time.time() - self.start_time
            self.remain_time = max(0, TRIAL_SECONDS - elapsed)
            
            if self.remain_time <= 0:
                self.running_status = "stop"
                logger.warning("License expired. Shutting down...")
                self.request_shutdown()
                

    def start(self) -> None:
        self.start_time = time.time()
        self.license_status = self.check_license()
        self._tick_license()
        if self.license_status:
            logger.info("Running on Unlimited License")
        else:
            logger.info("Running on Trial License, remain time: %s seconds", self.remain_time)

        self._initMelsecHandlers()
        self.itServiceHandler = ItServiceHandler(self.cfg)
        self.start_tags_loop()

        self.web = WebAPI(self)
        self.web.run_daemon()
        logger.info("Web UI: http://%s:%s", WEB_BIND_HOST, WEB_BIND_PORT)

        while not self._shutdown.is_set():
            self._tick_license()
            time.sleep(1)

        logger.info("Shutdown requested. Cleaning up...")
        self.stop_tags_loop()
        self.disconnect_data_services()

    def request_shutdown(self) -> None:
        self._shutdown.set()

if __name__ == "__main__":
    controller = None
    try:
        controller = Controller()

        def _handle_stop(signum, frame):
            logger.info("Received signal %s, requesting shutdown...", signum)
            try:
                controller.request_shutdown()
            except Exception:
                pass

        for _name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            _sig = getattr(signal, _name, None)
            if _sig is None:
                continue
            try:
                signal.signal(_sig, _handle_stop)
            except Exception:
                pass

        controller.start()
    except KeyboardInterrupt:
        logger.info("Shutdown by user.")
    except Exception as e:
        logger.critical("Crashed: %s", e)
