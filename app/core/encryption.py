# app/core/encryption.py
from cryptography.fernet import Fernet
from app.core.config import settings
import base64
import logging

logger = logging.getLogger(__name__)

try:
    # Use a URL-safe base64 encoded key derived from the SECRET_KEY
    # Fernet key must be 32 bytes long.
    key = base64.urlsafe_b64encode(settings.SECRET_KEY.encode()[:32])
    fernet = Fernet(key)
except Exception as e:
    logger.error(f"Failed to initialize Fernet for encryption: {e}")
    fernet = None

def encrypt_data(data: str) -> str:
    """Encrypts a string."""
    if not fernet:
        raise RuntimeError("Encryption service is not initialized.")
    if not data:
        return ""
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """Decrypts a string."""
    if not fernet:
        raise RuntimeError("Encryption service is not initialized.")
    if not encrypted_data:
        return ""
    try:
        return fernet.decrypt(encrypted_data.encode()).decode()
    except Exception:
        # If decryption fails (e.g., key changed, data corrupted), return empty
        logger.warning("Failed to decrypt data. Key may have changed or data is invalid.")
        return ""
