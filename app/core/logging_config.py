# app/core/logging_config.py
"""
Centralised logging configuration for the Ollama Proxy Server.

The configuration is used both when the app is started directly
(e.g. `uvicorn app.main:app`) and when it is launched via Gunicorn.
It follows the standard ``logging.config.dictConfig`` schema,
including a proper ``root`` logger definition, which resolves the
“Unable to configure root logger” error.
"""

import logging
import logging.config
import sys
from pythonjsonlogger import jsonlogger

# ----------------------------------------------------------------------
# JSON formatter – adds a timestamp and forces the level name to be upper‑case
# ----------------------------------------------------------------------
class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """
    A thin wrapper around ``pythonjsonlogger.JsonFormatter`` that ensures
    a ``timestamp`` field is always present and that the ``level`` field
    is upper‑cased.
    """

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        # ``record.created`` is a float epoch timestamp – we keep it as‑is
        if not log_record.get("timestamp"):
            log_record["timestamp"] = record.created

        # Normalise the level name (e.g. ``info`` → ``INFO``)
        log_record["level"] = (log_record.get("level") or record.levelname).upper()

# ----------------------------------------------------------------------
# Helper that builds the dict‑config structure.
# ----------------------------------------------------------------------
def _build_logging_config(log_level: str = "INFO") -> dict:
    """
    Returns a ``dict`` compatible with ``logging.config.dictConfig``.
    ``log_level`` can be any standard logging level name (case‑insensitive).
    """
    level = log_level.upper()

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "app.core.logging_config.CustomJsonFormatter",
                "format": "%(timestamp)s %(level)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            },
        },
        # ------------------------------------------------------------------
        # NOTE: The *root* logger must be defined with the key ``"root"``,
        # not an empty string.  This is what caused the original error.
        # ------------------------------------------------------------------
        "root": {
            "level": level,
            "handlers": ["default"],
        },
        # Additional loggers (uvicorn, gunicorn, etc.) inherit from the root
        "loggers": {
            "uvicorn.error": {"level": level, "propagate": False, "handlers": ["default"]},
            "uvicorn.access": {"level": level, "propagate": False, "handlers": ["default"]},
            "gunicorn.error": {"level": level, "propagate": False, "handlers": ["default"]},
            "gunicorn.access": {"level": level, "propagate": False, "handlers": ["default"]},
        },
    }

# ----------------------------------------------------------------------
# Public API – configure logging once (or re‑configure safely)
# ----------------------------------------------------------------------
def setup_logging(log_level: str = "INFO") -> None:
    """
    Apply the logging configuration.

    This function can be called multiple times (e.g. during tests or
    when the app is started both via ``uvicorn`` and via ``gunicorn``);
    each call simply re‑applies the dict configuration.
    """
    config = _build_logging_config(log_level)
    logging.config.dictConfig(config)

# ----------------------------------------------------------------------
# Export a ready‑to‑use config for Gunicorn (imported in ``gunicorn_conf.py``)
# ----------------------------------------------------------------------
# The environment variable ``LOG_LEVEL`` (set by the Dockerfile / .env)
# determines the default level for the server process.
LOGGING_CONFIG = _build_logging_config()
