import logging
import sys
from pythonjsonlogger import jsonlogger

LOG_LEVEL = "INFO"

class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super(CustomJsonFormatter, self).add_fields(log_record, record, message_dict)
        if not log_record.get('timestamp'):
            log_record['timestamp'] = record.created
        if log_record.get('level'):
            log_record['level'] = log_record['level'].upper()
        else:
            log_record['level'] = record.levelname

def setup_logging(log_level=LOG_LEVEL):
    logger = logging.getLogger()
    # Ensure the log level is uppercase
    logger.setLevel(log_level.upper())
    
    # Prevent duplicate logs in Uvicorn
    for handler in logger.handlers:
        logger.removeHandler(handler)

    logHandler = logging.StreamHandler(sys.stdout)
    formatter = CustomJsonFormatter('%(timestamp)s %(level)s %(name)s %(message)s')
    logHandler.setFormatter(formatter)
    
    logger.addHandler(logHandler)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "app.core.logging_config.CustomJsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s %(lineno)d %(message)s",
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": True},
        "uvicorn.error": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": False},
        "uvicorn.access": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": False},
        "gunicorn.error": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": False},
        "gunicorn.access": {"handlers": ["default"], "level": LOG_LEVEL, "propagate": False},
    },
}