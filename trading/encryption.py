"""
Fernet-based encrypted field for storing secrets in the database.
The encryption key comes from settings.FIELD_ENCRYPTION_KEY (set in .env).
"""
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode()
                  if isinstance(settings.FIELD_ENCRYPTION_KEY, str)
                  else settings.FIELD_ENCRYPTION_KEY)


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string → base64 ciphertext string."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64 ciphertext string → plaintext."""
    return _fernet().decrypt(ciphertext.encode()).decode()


class EncryptedCharField(models.TextField):
    """
    A Django model field that transparently encrypts on save and decrypts on load.
    Stored as ciphertext in the DB; plaintext is returned when the attribute is accessed.
    """

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return decrypt(value)
        except Exception:
            return value  # return raw if decryption fails (e.g. already plain)

    def to_python(self, value):
        return value  # already decrypted by from_db_value

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        # Only encrypt if not already ciphertext (idempotent on plain values)
        try:
            decrypt(value)
            return value  # already encrypted
        except Exception:
            return encrypt(value)
