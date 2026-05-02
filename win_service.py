from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import win32event
import win32service
import win32serviceutil


def _service_boot_log_paths() -> list[Path]:
    """不依賴 AppLogger；多路徑寫入便於權限／路徑問題時仍可取證。"""
    paths: list[Path] = []
    try:
        if getattr(sys, "frozen", False):
            paths.append(Path(sys.executable).resolve().parent / "logs" / "service_boot.log")
        else:
            paths.append(Path(__file__).resolve().parent / "logs" / "service_boot.log")
    except Exception:
        pass
    try:
        paths.append(Path(tempfile.gettempdir()) / "platinum_melsec_bridge" / "service_boot.log")
    except Exception:
        pass
    try:
        paths.append(
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "platinum" / "logs" / "service_boot.log"
        )
    except Exception:
        pass

    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _append_service_boot(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n"
    ok_any = False
    last_err: str | None = None
    for path in _service_boot_log_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            ok_any = True
        except Exception as e:
            last_err = f"{path}: {e}"
    if not ok_any and last_err:
        try:
            sys.stderr.write(f"[melsecBridge service_boot] FAILED: {last_err}\n")
        except Exception:
            pass


def _set_working_dir_to_exe_dir() -> None:
    """SCM 預設 CWD 常為 system32；切到 exe 目錄以利 config.yaml / logs。"""
    try:
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir:
            os.chdir(exe_dir)
    except Exception:
        pass


class MelsecBridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = "MelsecBridge"
    _svc_display_name_ = "NeoEdge Melsec Bridge"
    _svc_description_ = "PLC polling bridge with Web UI (FastAPI/Uvicorn)."

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.controller: Any = None
        self._worker: threading.Thread | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        try:
            if self.controller is not None:
                self.controller.request_shutdown()
        except Exception:
            pass
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        _set_working_dir_to_exe_dir()
        _append_service_boot(
            f"SvcDoRun enter frozen={getattr(sys, 'frozen', False)} exe={sys.executable} cwd={os.getcwd()}"
        )

        try:
            from logHelper import logger as app_logger
        except Exception:
            _append_service_boot(
                "Failed to import logHelper logger:\n" + traceback.format_exc()
            )
            raise

        app_logger.info("Service starting")

        try:
            from main import Controller
        except Exception:
            _append_service_boot(
                "Failed to import main.Controller:\n" + traceback.format_exc()
            )
            raise

        try:
            self.controller = Controller()

            def _run_controller() -> None:
                try:
                    assert self.controller is not None
                    self.controller.start()
                except BaseException as e:
                    _append_service_boot(
                        "Controller.start crashed:\n"
                        + "".join(traceback.format_exception(e))
                    )

            # ServiceFramework.SvcRun 已對 SCM 宣告 RUNNING；背景跑主迴圈可避免阻塞。
            self._worker = threading.Thread(target=_run_controller, name="controller", daemon=True)
            self._worker.start()

            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

            try:
                if self.controller is not None:
                    self.controller.request_shutdown()
            except Exception:
                pass

            if self._worker is not None:
                self._worker.join(timeout=90.0)
        except Exception:
            msg = "Service crashed:\n" + traceback.format_exc()
            _append_service_boot(msg)
            try:
                app_logger.critical(msg)
            except Exception:
                pass
            time.sleep(1.0)
            raise
        finally:
            try:
                app_logger.info("Service stopped")
            except Exception:
                pass


if __name__ == "__main__":
    try:
        _append_service_boot(f"__main__ argv={sys.argv} frozen={getattr(sys, 'frozen', False)}")
    except Exception:
        pass

    try:
        # HandleCommandLine：len(argv)<=1 會 usage()+exit。SCM 啟動只有 exe，須走 dispatcher。
        if len(sys.argv) <= 1:
            import servicemanager

            _append_service_boot("SCM: Initialize / PrepareToHostSingle / StartServiceCtrlDispatcher")
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(MelsecBridgeService)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            win32serviceutil.HandleCommandLine(MelsecBridgeService)
    except Exception:
        try:
            _append_service_boot("__main__ crashed:\n" + traceback.format_exc())
        except Exception:
            pass
        raise
