import os
from app.core.logging_config import LOGGING_CONFIG

# Gunicorn config variables
loglevel = os.environ.get("LOG_LEVEL", "info")
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8080")
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "uvicorn.workers.UvicornWorker")
accesslog = "-"  # Direct access logs to stdout
errorlog = "-"   # Direct error logs to stdout

# Use our custom JSON logging config
logconfig_dict = LOGGING_CONFIG