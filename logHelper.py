# logger_config.py
import logging
import os
import sys
from pathlib import Path

import yaml
from logging.handlers import TimedRotatingFileHandler

# 若 exe 目錄無法建立 logs（例如權限），改指向此目錄（僅本 process）
_logs_dir_override: Path | None = None

def app_base_dir() -> Path:
    """PyInstaller onefile 時為 exe 所在目錄；開發模式為本檔所在目錄（專案根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def get_logs_dir() -> Path:
    if _logs_dir_override is not None:
        return _logs_dir_override
    if getattr(sys, "frozen", False):
        return app_base_dir() / "logs"
    return Path("logs")

def _ensure_logs_dir() -> Path:
    """建立可寫入的 logs 目錄；必要時 fallback 到 ProgramData 或 cwd/logs。"""
    global _logs_dir_override

    log_root = get_logs_dir()
    try:
        os.makedirs(log_root, exist_ok=True)
        return log_root
    except Exception:
        pass

    candidates = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "platinum" / "logs",
        Path.cwd() / "logs",
    ]
    for fb in candidates:
        try:
            os.makedirs(fb, exist_ok=True)
            _logs_dir_override = fb.resolve()
            return _logs_dir_override
        except Exception:
            continue
    raise OSError("無法建立 logs 目錄（exe 旁、ProgramData、cwd/logs 皆失敗）")

def _resolved_config_path() -> str:
    """與 main.resolve_config_path 一致，避免 log 與 Controller 讀不同檔案。"""
    primary = Path("/config.yaml")
    if primary.exists():
        return str(primary)
    if getattr(sys, "frozen", False):
        return str(app_base_dir() / "config.yaml")
    return str(Path("config.yaml"))

class AppLogger:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or _resolved_config_path()
        _ensure_logs_dir()

        cfg_path = Path(self.config_path)
        if not cfg_path.is_file():
            self._setup_bootstrap_logger()
            self.logger.critical(
                "Config file not found at %s (resolved=%s). "
                "Place config.yaml next to the executable or use C:\\config.yaml.",
                cfg_path,
                self.config_path,
            )
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        except Exception as e:
            self._setup_bootstrap_logger()
            self.logger.critical(
                "Failed to read/parse config %s: %s", self.config_path, e
            )
            return

        self.app_name = self.config.get("app", {}).get("app_name", "app")
        log_level_str = self.config.get("app", {}).get("log_level", "INFO")
        self.log_level = getattr(logging, log_level_str, logging.INFO)

        self.logger = logging.getLogger(self.app_name)
        self.logger.setLevel(self.log_level)

        if not self.logger.handlers:
            self._add_console_handler()
            self._add_file_handler()

    def _setup_bootstrap_logger(self) -> None:
        """設定檔不可用時：避免 import 階段崩潰，僅 console + bootstrap.log。"""
        self.config = {"app": {}}
        self.app_name = "app"
        self.log_level = logging.INFO
        self.logger = logging.getLogger(self.app_name)
        self.logger.setLevel(self.log_level)
        if not self.logger.handlers:
            self._add_console_handler()
            self._add_bootstrap_file_handler()

    def _add_console_handler(self) -> None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(console_handler)

    def _add_file_handler(self) -> None:
        log_root = get_logs_dir()
        backup_count = int(self.config.get("app", {}).get("log_file_count", 5))
        file_handler = TimedRotatingFileHandler(
            str(log_root / "app.log"),
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(self.log_level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(file_handler)

    def _add_bootstrap_file_handler(self) -> None:
        log_root = get_logs_dir()
        fh = logging.FileHandler(str(log_root / "bootstrap.log"), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(fh)

    def get_logger(self) -> logging.Logger:
        return self.logger

def reconfigure_logging_from_cfg(cfg: dict) -> None:
    """
    軟重載後依新 cfg 的 app.* 更新現有 logger 的等級與檔案輪替數量。
    （logger 名稱仍為啟動時的 app_name，若改名僅更新 level／backupCount。）
    """
    app = cfg.get("app") or {}
    level_name = str(app.get("log_level", "INFO"))
    new_level = getattr(logging, level_name, logging.INFO)
    lg = logger
    lg.setLevel(new_level)
    for h in lg.handlers:
        h.setLevel(new_level)
        if isinstance(h, TimedRotatingFileHandler):
            try:
                h.backupCount = max(1, int(app.get("log_file_count", 5)))
            except (TypeError, ValueError):
                pass

logger = AppLogger().get_logger()
