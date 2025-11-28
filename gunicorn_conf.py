import os
import json
import sqlalchemy
from pathlib import Path
from app.core.logging_config import LOGGING_CONFIG
from app.core.config import settings


# Gunicorn config variables
loglevel = os.environ.get("LOG_LEVEL", "info")
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8080")
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "uvicorn.workers.UvicornWorker")
accesslog = "-"  # Direct access logs to stdout
errorlog = "-"   # Direct error logs to stdout

# Use our custom JSON logging config
logconfig_dict = LOGGING_CONFIG

# --- Load SSL settings from DB for Gunicorn ---
keyfile = None
certfile = None

try:
    # Construct synchronous DB URL from bootstrap settings
    if settings.DATABASE_URL.startswith("sqlite+aiosqlite"):
        db_path = settings.DATABASE_URL.split("///")[-1]
        
        if Path(db_path).exists():
            sync_db_url = f"sqlite:///{db_path}"
            engine = sqlalchemy.create_engine(sync_db_url)
            with engine.connect() as connection:
                result = connection.execute(sqlalchemy.text("SELECT settings_data FROM app_settings WHERE id = 1")).fetchone()
                if result and result[0]:
                    db_settings = json.loads(result[0])
                    keyfile_path = db_settings.get("ssl_keyfile")
                    certfile_path = db_settings.get("ssl_certfile")

                    if keyfile_path and certfile_path:
                        if Path(keyfile_path).is_file() and Path(certfile_path).is_file():
                            keyfile = keyfile_path
                            certfile = certfile_path
                            print(f"[INFO] Gunicorn starting with HTTPS. Cert: {certfile}")
                        else:
                            if not Path(keyfile_path).is_file():
                                print(f"[WARNING] SSL key file not found at '{keyfile_path}'. HTTPS disabled.")
                            if not Path(certfile_path).is_file():
                                print(f"[WARNING] SSL cert file not found at '{certfile_path}'. HTTPS disabled.")
except Exception as e:
    print(f"[INFO] Could not load SSL settings from DB (this is normal on first run). Reason: {e}")