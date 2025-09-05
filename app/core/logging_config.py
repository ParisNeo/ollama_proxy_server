# app/core/logging_config.py
"""
Centralised logging configuration for the Ollama Proxy Server.

Features
--------
* Human‑readable console output by default.
* Optional JSON output via the LOG_FORMAT env‑var (kept for backward‑compatibility).
* All loggers (root, uvicorn, gunicorn, etc.) share the same configuration.
* The `setup_logging()` helper can be called from any entry‑point (FastAPI,
  management scripts, tests) to guarantee a consistent format.
"""

import logging
import logging.config
import os
import sys
from datetime import datetime
from pythonjsonlogger import jsonlogger

# ----------------------------------------------------------------------
# Human‑readable formatter
# ----------------------------------------------------------------------
class HumanReadableFormatter(logging.Formatter):
    """
    Example output:
    2025-09-05 12:15:50,297 [ERROR] gunicorn.error – Worker (pid:590871) was sent SIGINT!
    """
    DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s – %(message)s"
    DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S,%f"

    def __init__(self):
        # ``asctime`` already includes microseconds; we trim to milliseconds
        super().__init__(self.DEFAULT_FORMAT, self.DEFAULT_DATEFMT)

# ----------------------------------------------------------------------
# JSON formatter – kept for compatibility
# ----------------------------------------------------------------------
class JsonFormatter(jsonlogger.JsonFormatter):
    """
    Emits the same fields that the original configuration emitted.
    """
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        # Ensure a numeric epoch timestamp like the previous version
        if not log_record.get("timestamp"):
            log_record["timestamp"] = record.created
        # Normalise level name to upper‑case
        log_record["level"] = (log_record.get("level") or record.levelname).upper()

# ----------------------------------------------------------------------
# Build the dictConfig – selects formatter based on LOG_FORMAT env‑var
# ----------------------------------------------------------------------
def _build_logging_config(log_level: str = "INFO") -> dict:
    """
    Returns a ``dict`` compatible with ``logging.config.dictConfig``.
    ``log_level`` can be any standard level name (case‑insensitive).
    """
    level = log_level.upper()

    # Choose which formatter to use (human‑readable is default)
    fmt_type = os.getenv("LOG_FORMAT", "human").lower()
    if fmt_type == "json":
        formatter_name = "json"
        formatter_cfg = {
            "()": "app.core.logging_config.JsonFormatter",
            "format": "%(timestamp)s %(level)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s",
        }
    else:  # human‑readable
        formatter_name = "human"
        formatter_cfg = {
            "()": "app.core.logging_config.HumanReadableFormatter"
        }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "human": formatter_cfg,
            "json": formatter_cfg,
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": formatter_name,
                "stream": "ext://sys.stdout",
            },
        },
        # ---------- ROOT LOGGER ----------
        "root": {
            "level": level,
            "handlers": ["default"],
        },
        # ---------- OTHER LOGGERS ----------
        "loggers": {
            # Explicitly configure the popular libraries so they don’t add extra handlers
            "uvicorn.error": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": ["default"], "level": level, "propagate": False},
            "gunicorn.error": {"handlers": ["default"], "level": level, "propagate": False},
            "gunicorn.access": {"handlers": ["default"], "level": level, "propagate": False},
        },
    }

# ----------------------------------------------------------------------
# Public objects used by the rest of the codebase
# ----------------------------------------------------------------------
# Default config imported by gunicorn_conf.py
LOGGING_CONFIG = _build_logging_config()

def setup_logging(log_level: str = "INFO") -> None:
    """
    Apply the logging configuration.  It can be called multiple times;
    each call simply re‑applies the dict configuration.
    """
    config = _build_logging_config(log_level)
    logging.config.dictConfig(config)

# Exported names for ``from app.core.logging_config import …``
__all__ = ["setup_logging", "LOGGING_CONFIG", "HumanReadableFormatter", "JsonFormatter"]
