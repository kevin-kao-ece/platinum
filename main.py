import sys
import threading
import time
import signal
import yaml
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from itService import ItServiceHandler
from logHelper import app_base_dir, logger, reconfigure_logging_from_cfg
from melsec import MelsecHandler, max_connections_from_config
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
        self.melsecHandler = None
        self.itServiceHandler = None
        self.executor: ThreadPoolExecutor | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
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
        t = self._poll_thread
        if t is not None and t.is_alive():
            t.join(timeout=60.0)
            if t.is_alive():
                logger.warning("輪詢執行緒在逾時後仍未結束")
        self._poll_thread = None
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

        if self.melsecHandler is not None:
            try:
                self.melsecHandler.close()
            except Exception as e:
                logger.warning("關閉 Melsec 時例外: %s", e)
            self.melsecHandler = None

    def reconnect_data_services(self) -> None:
        """依目前 self.cfg 建立新的 ItServiceHandler。"""
        self.itServiceHandler = ItServiceHandler(self.cfg)

    def _initMelsecHandler(self) -> None:
        melsec = self.cfg.get("melsec", {})
        try:
            self.melsecHandler = MelsecHandler(melsec["name"], melsec)
            n = self.melsecHandler.max_connections
            logger.info(
                "Initialized MELSEC PLC: %s at %s (%s connection session%s)",
                melsec["name"],
                melsec["ip"],
                n,
                "s" if n != 1 else "",
            )
        except Exception as e:
            melsec_name = melsec.get("name", "Unknown")
            logger.error("Failed to initialize PLC %s: %s", melsec_name, e)

    def start_tags_loop(self) -> None:
        """依 melsec.tags 建立 executor 並啟動輪詢執行緒。"""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            logger.warning("tags 輪詢已在執行中，略過重複啟動")
            return
        if self.executor is not None:
            logger.warning("executor 仍存在，請先 stop_tags_loop")
            return

        melsec_cfg = self.cfg.get("melsec", {})
        pool_size = (
            self.melsecHandler.max_connections
            if self.melsecHandler
            else max_connections_from_config(melsec_cfg)
        )
        self.executor = ThreadPoolExecutor(max_workers=pool_size)
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._run_polling_loop,
            name="tags-loop",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info(
            "PLC polling thread started (pool size = %s).",
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
                self._initMelsecHandler()
                self.start_tags_loop()
                self.running_status = "running"
                logger.info("重載完成")
            except Exception as e:
                self.running_status = "error"
                logger.critical("重載失敗: %s", e)
                raise

    def _run_polling_loop(self) -> None:
        poll_interval = self.cfg.get("melsec", {}).get("poll_interval", 3.0)
        self.running_status = "running"
        while not self._poll_stop.is_set():
            start_time = time.time()
            logger.debug("Start polling loop at %s", start_time)

            current_loop_data: dict = {}
            current_loop_data["name"] = self.cfg.get("melsec", {}).get("name")

            futures = []
            tags = self.cfg.get("melsec", {}).get("tags", {})
            for tag_name, details in tags.items():
                if self._poll_stop.is_set():
                    break
                f = self.executor.submit(
                    self.poll_tag, tag_name, details, current_loop_data
                )
                futures.append(f)

            for f in futures:
                if self._poll_stop.is_set():
                    break
                f.result()

            if self._poll_stop.is_set():
                break

            if self.itServiceHandler:
                try:
                    self.itServiceHandler.insertMessageToInfluxDB(current_loop_data)
                    self.itServiceHandler.insertMessageToMongoDB(current_loop_data)
                except Exception as e:
                    logger.error("資料後端寫入例外（略過本輪，繼續輪詢）: %s", e)

            elapsed = time.time() - start_time
            wait = max(0.0, poll_interval - elapsed)
            end = time.time() + wait
            while time.time() < end and not self._poll_stop.is_set():
                time.sleep(0.05)

        logger.info("輪詢迴圈已結束（stop_tags_loop）")

    def poll_tag(self, tag_name: str, details: dict, result_dict: dict) -> None:
        if self.melsecHandler:
            try:
                val = self.melsecHandler.read(tag_name, details)
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

        self._initMelsecHandler()
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
