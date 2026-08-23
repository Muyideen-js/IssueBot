import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    configured = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
    if configured:
        try:
            return Fernet(configured.encode())
        except ValueError:
            pass

    if os.getenv("APP_ENV", "development").lower() == "production":
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key in production")

    seed = configured or os.getenv("APP_SECRET", "issuebot-development-only")
    derived = base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest())
    return Fernet(derived)


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored credentials cannot be decrypted with the configured key") from exc
