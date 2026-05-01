# logger_config.py
import logging
import os
from pathlib import Path

import yaml
from logging.handlers import TimedRotatingFileHandler


def _resolved_config_path() -> str:
    """與 main.resolve_config_path 一致，避免 log 與 Controller 讀不同檔案。"""
    primary = Path("/config.yaml")
    if primary.exists():
        return str(primary)
    return str(Path("config.yaml"))


class AppLogger:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or _resolved_config_path()
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.app_name = self.config.get("app", {}).get("app_name", "app")
        log_level_str = self.config.get("app", {}).get("log_level", "INFO")
        self.log_level = getattr(logging, log_level_str, logging.INFO)

        os.makedirs("logs", exist_ok=True)

        self.logger = logging.getLogger(self.app_name)
        self.logger.setLevel(self.log_level)

        if not self.logger.handlers:
            self._add_console_handler()
            self._add_file_handler()

    def _add_console_handler(self) -> None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        console_format = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(console_format)
        self.logger.addHandler(console_handler)

    def _add_file_handler(self) -> None:
        backup_count = int(self.config.get("app", {}).get("log_file_count", 5))
        file_handler = TimedRotatingFileHandler(
            "logs/app.log",
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(self.log_level)
        file_format = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_format)
        self.logger.addHandler(file_handler)

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


# Log 統一由此 Instance 管理，其他程式不應該自行建立 instance
logger = AppLogger().get_logger()
