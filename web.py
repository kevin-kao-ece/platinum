
from __future__ import annotations
import asyncio
import copy
import csv
import io
import re
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
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
    for key in ("app", "influxdb", "mongodb"):
        if key not in data or not isinstance(data[key], dict):
            raise ValueError(f"缺少或無效的區段: {key}")

    melsecs_list = data.get("melsecs")
    legacy = data.get("melsec")

    if isinstance(melsecs_list, list) and len(melsecs_list) > 0:
        if len(melsecs_list) > 2:
            raise ValueError("melsecs 最多支援 2 台 PLC")
        for i, plc in enumerate(melsecs_list):
            if not isinstance(plc, dict):
                raise ValueError(f"melsecs[{i}] 必須為 mapping")
            if plc.get("name") in (None, "") or plc.get("ip") in (None, ""):
                raise ValueError(f"melsecs[{i}] 必須包含有效的 name 與 ip")
            tags = plc.get("tags")
            if not isinstance(tags, dict) or not tags:
                raise ValueError(f"melsecs[{i}].tags 必須為非空 mapping")
        return data

    if isinstance(legacy, dict) and legacy:
        tags = legacy.get("tags")
        if not isinstance(tags, dict) or not tags:
            raise ValueError("melsec.tags 必須為非空 mapping")
        return data

    raise ValueError("必須提供 melsec（單台）或 melsecs（1～2 台）")


def _defaults_plc_slot() -> dict[str, Any]:
    return {
        "name": "",
        "ip": "",
        "port": 6001,
        "frame_type": "3E",
        "max_connections": 1,
        "poll_interval": 5.0,
        "connect_retries": 3,
        "connect_retry_delay_sec": 0.5,
        "tags": {},
    }


def _normalize_tag_header(key: str) -> str:
    k = (key or "").strip().lower().replace(" ", "_")
    aliases = {
        "tag": "tag_name",
        "tagname": "tag_name",
        "name": "tag_name",
    }
    return aliases.get(k, k)


# 與 melsec.MelsecSession（read/write）支援之 datatype 一致
_TAG_DATATYPES_SUPPORTED = frozenset(
    {"bool", "string", "uint16", "int16", "uint32", "int32", "float", "double"}
)


def _normalize_and_validate_melsec_device(device_raw: str, tag_name: str) -> str:
    """以 pymcprotocol（Q 系列，與 Type3E() 預設相同）驗證裝置字串並回傳可用寫法。"""
    try:
        from pymcprotocol.type3e import Type3E
    except ImportError as e:
        raise ValueError("伺服器缺少 pymcprotocol，無法驗證 device") from e

    d = device_raw.strip()
    if not d:
        raise ValueError(f"標籤「{tag_name}」device 不可為空")

    plc = Type3E("Q")
    last_err: BaseException | None = None
    for cand in (d, d.upper()):
        try:
            plc._make_devicedata(cand)
            return cand
        except BaseException as e:
            last_err = e
    raise ValueError(
        f"標籤「{tag_name}」device「{d}」不符合 MC Protocol（三菱 MC／本程式預設 Q 系列）"
    ) from last_err


def _validate_device_matches_datatype(tag_name: str, device: str, dtype: str) -> None:
    """位元裝置僅允許 bool；字／雙字裝置允許 string 與數值型（見 melsec 使用 batchread_bitunits vs wordunits）。"""
    try:
        from pymcprotocol.mcprotocolconst import DeviceConstants
    except ImportError as e:
        raise ValueError("伺服器缺少 pymcprotocol，無法驗證 device") from e

    m = re.search(r"\D+", device)
    if not m:
        raise ValueError(f"標籤「{tag_name}」device「{device}」無法辨識裝置字首")
    devicetype = m.group(0).upper()

    try:
        kind = DeviceConstants.get_devicetype("Q", devicetype)
    except Exception as e:
        raise ValueError(
            f"標籤「{tag_name}」device 字首「{devicetype}」非支援之裝置區：{e}"
        ) from e

    bit_k = DeviceConstants.BIT_DEVICE
    word_k = DeviceConstants.WORD_DEVICE
    dword_k = DeviceConstants.DWORD_DEVICE

    if dtype == "bool":
        if kind != bit_k:
            raise ValueError(
                f"標籤「{tag_name}」datatype 為 bool 時，device 須為位元區（如 X/Y/M/L/B…），目前為「{device}」"
            )
    else:
        if kind not in (word_k, dword_k):
            raise ValueError(
                f"標籤「{tag_name}」datatype 為「{dtype}」時，device 須為字組／雙字組區（如 D/W/R/ZR/SW…），目前為「{device}」"
            )


def _validate_csv_tag_row(tag_name: str, access_raw: str, device_raw: str, datatype_raw: str) -> dict[str, Any]:
    access = access_raw.strip().lower()
    if access not in ("read", "write"):
        raise ValueError(f"標籤「{tag_name}」access 須為 read 或 write（不可為「{access_raw.strip()}」）")

    dtype = datatype_raw.strip().lower()
    if dtype not in _TAG_DATATYPES_SUPPORTED:
        allowed = ", ".join(sorted(_TAG_DATATYPES_SUPPORTED))
        raise ValueError(
            f"標籤「{tag_name}」datatype「{datatype_raw.strip()}」不支援；請使用：{allowed}"
        )

    device_norm = _normalize_and_validate_melsec_device(device_raw, tag_name)
    _validate_device_matches_datatype(tag_name, device_norm, dtype)

    return {"access": access, "device": device_norm, "datatype": dtype}


def _tags_dict_from_csv_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    for row in rows:
        norm = {_normalize_tag_header(k): (v or "").strip() for k, v in row.items() if k}
        name = norm.get("tag_name", "").strip()
        if not name:
            continue
        device = norm.get("device", "").strip()
        datatype = norm.get("datatype", "").strip()
        if not device:
            raise ValueError(f"標籤「{name}」缺少 device")
        if not datatype:
            raise ValueError(f"標籤「{name}」缺少 datatype")
        access = norm.get("access", "").strip()
        tags[name] = _validate_csv_tag_row(name, access, device, datatype)
    if not tags:
        raise ValueError("CSV 無有效標籤列（需含 tag_name, access, device, datatype）")
    return tags


def _ensure_melsecs_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    raw_list = cfg.get("melsecs")
    if isinstance(raw_list, list) and raw_list:
        out = [x for x in raw_list if isinstance(x, dict)]
        cfg["melsecs"] = out
        if "melsec" in cfg:
            del cfg["melsec"]
        return cfg["melsecs"]

    single = cfg.get("melsec")
    if isinstance(single, dict) and single:
        cfg["melsecs"] = [copy.deepcopy(single)]
        del cfg["melsec"]
        return cfg["melsecs"]

    raise ValueError("設定中無有效的 melsec / melsecs")


def _ensure_second_plc_stub(cfg: dict[str, Any]) -> None:
    m = _ensure_melsecs_list(cfg)
    if len(m) >= 2:
        return
    first = m[0]
    base_tags = copy.deepcopy(first.get("tags") or {})
    if not base_tags:
        raise ValueError("第一台 PLC 無標籤，無法自動建立第二台（請先設定標籤）")
    two_name = "PLC_02"
    names = {str(x.get("name", "")) for x in m}
    if two_name in names:
        two_name = "PLC_02b"
    m.append(
        {
            "name": two_name,
            "ip": "",
            "port": int(first.get("port", 6001) or 6001),
            "frame_type": str(first.get("frame_type", "3E") or "3E"),
            "max_connections": int(first.get("max_connections", 1) or 1),
            "poll_interval": float(first.get("poll_interval", 5.0) or 5.0),
            "connect_retries": int(first.get("connect_retries", 3) or 3),
            "connect_retry_delay_sec": float(
                first.get("connect_retry_delay_sec", 0.5) or 0.5
            ),
            "tags": base_tags,
        }
    )


def _plc_slot_to_api(plc: dict[str, Any] | None, defaults: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plc, dict):
        plc = {}
    tags = plc.get("tags")
    if not isinstance(tags, dict):
        tags = {}
    base = copy.deepcopy(defaults)
    base.update(
        {
            "name": plc.get("name") or base["name"],
            "ip": plc.get("ip") or "",
            "port": plc.get("port", base["port"]),
            "frame_type": str(plc.get("frame_type", base["frame_type"]) or "3E"),
            "max_connections": int(plc.get("max_connections", base["max_connections"]) or 1),
            "poll_interval": float(plc.get("poll_interval", base["poll_interval"]) or 5.0),
            "connect_retries": int(plc.get("connect_retries", base["connect_retries"]) or 3),
            "connect_retry_delay_sec": float(
                plc.get("connect_retry_delay_sec", base["connect_retry_delay_sec"]) or 0.5
            ),
            "tags": tags,
        }
    )
    return base


def _build_setup_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    defaults = _defaults_plc_slot()
    plcs: list[dict[str, Any]] = []
    try:
        plcs = _ensure_melsecs_list(copy.deepcopy(cfg))
    except ValueError:
        plcs = []

    plc1 = _plc_slot_to_api(plcs[0] if len(plcs) > 0 else None, {**defaults, "name": "PLC_01"})
    plc2 = _plc_slot_to_api(plcs[1] if len(plcs) > 1 else None, {**defaults, "name": "PLC_02"})

    influx = cfg.get("influxdb") if isinstance(cfg.get("influxdb"), dict) else {}
    mongo = cfg.get("mongodb") if isinstance(cfg.get("mongodb"), dict) else {}

    return {
        "plc1": plc1,
        "plc2": plc2,
        "influxdb": {
            "url": influx.get("url") or "",
            "token": influx.get("token") or "",
            "org": influx.get("org") or "",
            "bucket": influx.get("bucket") or "",
            "user": influx.get("user") or "",
            "password": influx.get("password") or "",
            "measurement": influx.get("measurement") or "",
        },
        "mongodb": {
            "host": mongo.get("host") or "",
            "port": int(mongo.get("port", 27017) or 27017),
            "database": mongo.get("database") or "",
            "collection": mongo.get("collection") or "",
            "user": mongo.get("user") or "",
            "password": mongo.get("password") or "",
        },
    }


def _apply_setup_payload(cfg: dict[str, Any], body: dict[str, Any]) -> None:
    plcs = _ensure_melsecs_list(cfg)

    def patch_plc(target: dict[str, Any], src: dict[str, Any] | None) -> None:
        if not isinstance(src, dict):
            return
        if src.get("name") not in (None, ""):
            target["name"] = str(src["name"]).strip()
        if "ip" in src:
            target["ip"] = str(src.get("ip") or "").strip()
        if "port" in src:
            target["port"] = int(src["port"])
        if "frame_type" in src:
            target["frame_type"] = str(src.get("frame_type") or "3E").strip()
        if "max_connections" in src:
            target["max_connections"] = max(1, int(src["max_connections"]))
        if "poll_interval" in src:
            target["poll_interval"] = float(src["poll_interval"])
        if "connect_retries" in src:
            target["connect_retries"] = max(1, int(src["connect_retries"]))
        if "connect_retry_delay_sec" in src:
            target["connect_retry_delay_sec"] = float(src["connect_retry_delay_sec"])

    plc1_body = body.get("plc1")
    plc2_body = body.get("plc2")
    if isinstance(plc2_body, dict) and any(
        plc2_body.get(k) not in (None, "")
        for k in ("ip", "name")
    ):
        _ensure_second_plc_stub(cfg)
        plcs = _ensure_melsecs_list(cfg)

    patch_plc(plcs[0], plc1_body if isinstance(plc1_body, dict) else None)
    if len(plcs) > 1:
        patch_plc(plcs[1], plc2_body if isinstance(plc2_body, dict) else None)

    influx_body = body.get("influxdb")
    if isinstance(influx_body, dict):
        influx = cfg.setdefault("influxdb", {})
        for key in (
            "url",
            "token",
            "org",
            "bucket",
            "user",
            "password",
            "measurement",
        ):
            if key in influx_body:
                influx[key] = influx_body[key]

    mongo_body = body.get("mongodb")
    if isinstance(mongo_body, dict):
        mongo = cfg.setdefault("mongodb", {})
        for key in ("host", "database", "collection", "user", "password"):
            if key in mongo_body:
                mongo[key] = mongo_body[key]
        if "port" in mongo_body:
            mongo["port"] = int(mongo_body["port"])


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
                "app_name": app_cfg.get("app_name") or "",
                "version": app_cfg.get("version") if app_cfg.get("version") is not None else "",
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

        @app.get("/api/config/setup")
        async def api_config_setup_get() -> dict[str, Any]:
            path = Path(ctrl.config_path)
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8")
                    cfg = yaml.safe_load(text)
                except OSError as e:
                    raise HTTPException(status_code=500, detail=f"無法讀取設定: {e}") from e
                except yaml.YAMLError as e:
                    raise HTTPException(status_code=500, detail=f"YAML 錯誤: {e}") from e
            else:
                cfg = copy.deepcopy(ctrl.cfg) if isinstance(ctrl.cfg, dict) else {}
            if not isinstance(cfg, dict):
                cfg = {}
            return _build_setup_snapshot(cfg)

        @app.put("/api/config/setup")
        async def api_config_setup_put(body: dict[str, Any] = Body(...)) -> dict[str, str]:
            path = Path(ctrl.config_path)
            if not path.parent.is_dir():
                path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                try:
                    raw = path.read_bytes()
                    cfg = yaml.safe_load(raw.decode("utf-8"))
                except UnicodeDecodeError as e:
                    raise HTTPException(status_code=500, detail=f"設定須為 UTF-8: {e}") from e
                except yaml.YAMLError as e:
                    raise HTTPException(status_code=500, detail=f"YAML 錯誤: {e}") from e
            else:
                cfg = copy.deepcopy(ctrl.cfg) if isinstance(ctrl.cfg, dict) else {}
            if not isinstance(cfg, dict):
                raise HTTPException(status_code=500, detail="設定根節點無效")

            try:
                _apply_setup_payload(cfg, body)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

            try:
                validate_platinum_config(cfg)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

            try:
                dump = yaml.safe_dump(
                    cfg,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                path.write_text(dump, encoding="utf-8")
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"無法寫入: {e}") from e

            logger.info("設定已由網頁「Setup」寫入 %s", path)
            return {"status": "success", "message": "設定已寫入，請按「重新啟動程式」套用"}

        @app.post("/api/config/plc/{plc_index}/tags/import")
        async def api_plc_tags_import(
            plc_index: int,
            file: UploadFile = File(...),
        ) -> dict[str, str]:
            if plc_index not in (0, 1):
                raise HTTPException(status_code=400, detail="plc_index 須為 0（PLC 1）或 1（PLC 2）")
            name_lower = (file.filename or "").lower()
            if not name_lower.endswith(".csv"):
                raise HTTPException(status_code=400, detail="請上傳 .csv")

            raw = await file.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as e:
                raise HTTPException(status_code=400, detail=f"檔案須為 UTF-8: {e}") from e

            try:
                reader = csv.DictReader(io.StringIO(text))
                if reader.fieldnames is None:
                    raise ValueError("CSV 無表頭")
                rows = list(reader)
                tags = _tags_dict_from_csv_rows(rows)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

            path = Path(ctrl.config_path)
            if path.is_file():
                try:
                    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
                except yaml.YAMLError as e:
                    raise HTTPException(status_code=500, detail=f"YAML 錯誤: {e}") from e
            else:
                cfg = copy.deepcopy(ctrl.cfg) if isinstance(ctrl.cfg, dict) else {}
            if not isinstance(cfg, dict):
                raise HTTPException(status_code=500, detail="設定根節點無效")

            try:
                if plc_index == 1:
                    _ensure_second_plc_stub(cfg)
                plcs = _ensure_melsecs_list(cfg)
                if plc_index >= len(plcs):
                    raise ValueError("PLC 索引超出目前設定中的台數")
                plcs[plc_index]["tags"] = tags
                validate_platinum_config(cfg)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

            try:
                dump = yaml.safe_dump(
                    cfg,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                path.write_text(dump, encoding="utf-8")
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"無法寫入: {e}") from e

            logger.info("PLC %s 標籤已由 CSV 匯入 %s", plc_index + 1, path)
            return {"status": "success", "message": "標籤已寫入設定檔，請重新啟動程式套用"}

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
