
from __future__ import annotations
import asyncio
import io
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from logHelper import get_logs_dir, logger

_ROOT = Path(__file__).resolve().parent


def _resource_path(relative: str) -> Path:
    try:
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    except Exception:
        base = _ROOT
    return base / relative


_STATIC = _resource_path("static")

try:
    from licenseHelp import (
        LICENSE_CRT_PATH,
        LICENSE_CSR_PATH,
        existing_license_crt_path,
        load_license_binding_meta,
    )
except ImportError:
    LICENSE_CRT_PATH = _ROOT / "license.crt"
    LICENSE_CSR_PATH = _ROOT / "license.csr"

    def load_license_binding_meta():  # type: ignore[misc]
        return None

    def existing_license_crt_path():  # type: ignore[misc]
        return LICENSE_CRT_PATH if LICENSE_CRT_PATH.is_file() else None

# 固定 Web 綁定位址（不讀 config.yaml）
WEB_BIND_HOST = "0.0.0.0"
WEB_BIND_PORT = 7001


def validate_platinum_config(data: Any) -> dict:
    """匯入前基本結構檢查。"""
    if not isinstance(data, dict):
        raise ValueError("設定根節點必須為 mapping")
    for key in ("app", "influxdb", "mongodb", "melsec"):
        if key not in data or not isinstance(data[key], dict):
            raise ValueError(f"缺少或無效的區段: {key}")
    tags = data["melsec"].get("tags")
    if not isinstance(tags, dict) or not tags:
        raise ValueError("melsec.tags 必須為非空 mapping")
    return data


class WSManager:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        for client in list(self.clients):
            try:
                await client.send_json(msg)
            except Exception:
                self.clients.discard(client)


class WebAPI:
    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self.app = FastAPI(title="Platinum Web")
        self.ws_mgr = WSManager()
        self._uvicorn_server: uvicorn.Server | None = None
        self._uvicorn_thread: threading.Thread | None = None
        if _STATIC.is_dir():
            self.app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
        self._build_routes()

    def run_daemon(self) -> None:
        """在背景執行緒啟動 uvicorn（埠固定 WEB_BIND_PORT），不阻塞 PLC 主迴圈。"""

        def _serve() -> None:
            try:
                config = uvicorn.Config(
                    self.app,
                    host=WEB_BIND_HOST,
                    port=WEB_BIND_PORT,
                    log_level="info",
                    install_signal_handlers=False,
                )
            except TypeError:
                config = uvicorn.Config(
                    self.app,
                    host=WEB_BIND_HOST,
                    port=WEB_BIND_PORT,
                    log_level="info",
                )
            server = uvicorn.Server(config)
            self._uvicorn_server = server
            asyncio.run(server.serve())

        self._uvicorn_thread = threading.Thread(
            target=_serve, name="uvicorn", daemon=True
        )
        self._uvicorn_thread.start()
        time.sleep(0.35)

    def shutdown_server(self, timeout: float = 15.0) -> None:
        """要求 uvicorn 結束並釋放連接埠，供程序重啟時子行程可 bind。"""
        server = self._uvicorn_server
        thread = self._uvicorn_thread
        if server is None or thread is None:
            logger.warning("shutdown_server: uvicorn 尚未初始化")
            return
        logger.info("正在關閉 Web 服務以釋放連接埠（重啟）…")
        server.should_exit = True
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "uvicorn 執行緒在 %.1fs 內未結束，後續子行程可能仍會遇到埠占用",
                timeout,
            )
        else:
            logger.info("Web 服務已關閉")

    def _build_routes(self) -> None:
        app = self.app
        ctrl = self.controller

        def safe_remain_time() -> int:
            v = getattr(ctrl, "remain_time", 0)
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            idx = _STATIC / "index.html"
            if idx.is_file():
                return idx.read_text(encoding="utf-8")
            raise HTTPException(status_code=404, detail="static/index.html not found")

        @app.get("/api/health")
        async def api_health() -> dict:
            return {"status": "ok"}

        @app.get("/api/status")
        async def api_status() -> dict:
            cfg = getattr(ctrl, "cfg", None)
            if not isinstance(cfg, dict):
                cfg = {}
            app_cfg = cfg.get("app") or {}
            ctrl_licensed = bool(getattr(ctrl, "license_status", False))
            crt_present = existing_license_crt_path() is not None
            # 與 main.check_license 一致：Controller 判定通過且憑證檔存在才算 Licensed
            licensed = ctrl_licensed and crt_present
            return {
                "running_status": getattr(ctrl, "running_status", "unknown"),
                "license_status": "Licensed" if licensed else "No License",
                "license_file_present": crt_present,
                "ctrl_license_status": ctrl_licensed,
                "remain_time": safe_remain_time(),
                "license_contact": app_cfg.get(
                    "license_contact", "貴司授權／業務窗口"
                ),
            }

        @app.get("/api/config/export")
        async def api_config_export():
            path = Path(ctrl.config_path)
            if not path.is_file():
                raise HTTPException(status_code=404, detail="找不到設定檔")
            return FileResponse(path, filename="config.yaml", media_type="application/x-yaml")

        # 相容舊路徑
        @app.get("/download_config")
        async def download_config_legacy():
            return await api_config_export()

        @app.post("/api/config/import")
        async def api_config_import(
            background_tasks: BackgroundTasks,
            file: UploadFile = File(...),
        ):
            name = (file.filename or "").lower()
            if not name.endswith((".yaml", ".yml")):
                raise HTTPException(status_code=400, detail="請上傳 .yaml / .yml")
            raw = await file.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise HTTPException(status_code=400, detail=f"檔案須為 UTF-8: {e}") from e
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as e:
                raise HTTPException(status_code=400, detail=f"YAML 錯誤: {e}") from e
            try:
                validate_platinum_config(data)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

            path = Path(ctrl.config_path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"無法寫入: {e}") from e

            logger.info("設定已由網頁匯入 %s", path)
            return {
                "status": "success",
                "message": "設定已寫入，請重新啟動程式",
            }

        @app.post("/upload_config")
        async def upload_config_legacy(
            background_tasks: BackgroundTasks,
            file: UploadFile = File(...),
        ):
            return await api_config_import(background_tasks, file)

        @app.post("/api/system/restart")
        async def api_restart(background_tasks: BackgroundTasks):
            logger.info("使用者觸發重新啟動程式")
            background_tasks.add_task(ctrl.soft_reload_runtime)
            return {"status": "reloading", "message": ""}

        @app.post("/restart")
        async def restart_legacy(background_tasks: BackgroundTasks):
            return await api_restart(background_tasks)

        @app.get("/api/logs/zip")
        async def api_logs_zip():
            log_dir = get_logs_dir()
            if not log_dir.is_dir():
                raise HTTPException(status_code=404, detail="無 logs 目錄")
            files = [p for p in log_dir.iterdir() if p.is_file()]
            if not files:
                raise HTTPException(status_code=404, detail="logs 內無檔案")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(files, key=lambda x: x.name):
                    zf.write(p, arcname=f"logs/{p.name}")
            return Response(
                content=buf.getvalue(),
                media_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="logs_export.zip"'},
            )

        @app.get("/download_logs_all")
        async def download_logs_legacy():
            return await api_logs_zip()

        @app.get("/api/logs/tail")
        async def api_logs_tail(lines: int = 100):
            log_file = get_logs_dir() / "app.log"
            if not log_file.is_file():
                return {"logs": ["尚無 logs/app.log"]}
            try:
                text = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as e:
                raise HTTPException(status_code=500, detail=str(e)) from e
            return {"logs": text[-lines:]}

        @app.get("/logs")
        async def logs_legacy():
            return await api_logs_tail(100)

        # --- License（占位；實作於 licenseHelp，日後可替換） ---
        try:
            from licenseHelp import LicenseHelper
        except ImportError:
            LicenseHelper = None  # type: ignore[misc, assignment]

        @app.get("/export_csr")
        async def export_csr():
            if LicenseHelper is None:
                raise HTTPException(status_code=503, detail="License 模組未安裝")
            try:
                lic = LicenseHelper()
                lic.genLicenseCSR(str(LICENSE_CSR_PATH))
            except Exception as e:
                logger.exception("export_csr failed: %s", e)
                raise HTTPException(status_code=503, detail=str(e)) from e
            if not LICENSE_CSR_PATH.is_file():
                raise HTTPException(status_code=500, detail="CSR 產生失敗")
            return FileResponse(
                LICENSE_CSR_PATH,
                filename="request.csr",
                media_type="application/pkcs10",
            )

        @app.post("/verify_license")
        async def verify_license(file: UploadFile = File(...)):
            if LicenseHelper is None:
                raise HTTPException(status_code=503, detail="License 模組未安裝")
            cert_bytes = await file.read()
            meta = load_license_binding_meta()
            if meta is None:
                return {
                    "status": "error",
                    "message": "缺少授權綁定資料（請先產生 CSR）",
                }
            try:
                lic = LicenseHelper()
            except RuntimeError as e:
                return {"status": "error", "message": str(e)}
            result = lic.verifyLicense(
                licenseCert=cert_bytes,
                encryptedAESKey=meta.get(
                    "encrypedAESKey", meta.get("encryptedAESKey", "")
                ),
                iv_hex=meta.get("iv_hex", ""),
            )
            if result.get("status"):
                LICENSE_CRT_PATH.parent.mkdir(parents=True, exist_ok=True)
                LICENSE_CRT_PATH.write_bytes(cert_bytes)
                ctrl.license_status = ctrl.check_license()
                return {"status": "success", "message": "License 驗證成功"}
            return {"status": "error", "message": result.get("desc", "驗證失敗")}

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await self.ws_mgr.connect(ws)
            await ws.send_json(
                {
                    "info": {
                        "status": ctrl.running_status,
                        "remainTime": safe_remain_time(),
                    }
                }
            )
            try:
                while True:
                    await ws.receive_text()
            except WebSocketDisconnect:
                self.ws_mgr.disconnect(ws)
