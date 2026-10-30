from sqlalchemy.orm import Session

from app.models import Setting
from app.services.secrets import Vault


def _upsert(db: Session, key: str, value: str, encrypted: bool) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value, encrypted=encrypted))
    else:
        row.value = value
        row.encrypted = encrypted
    db.commit()


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
    return default if row is None else row.value


def set_setting(db: Session, key: str, value: str) -> None:
    """Store `value` as plain text, replacing any secret saved under `key`."""
    _upsert(db, key, value, encrypted=False)


def get_secret(db: Session, vault: Vault, key: str) -> str | None:
    """Return the decrypted secret, or None if `key` isn't set.

    A value saved with `set_setting` isn't encrypted, so reading it here raises
    `InvalidToken`. So does a secret saved under a different key file.
    """
    row = db.get(Setting, key)
    return None if row is None else vault.decrypt(row.value)


def set_secret(db: Session, vault: Vault, key: str, value: str) -> None:
    _upsert(db, key, vault.encrypt(value), encrypted=True)


def delete_setting(db: Session, key: str) -> None:
    row = db.get(Setting, key)
    if row is not None:
        db.delete(row)
        db.commit()
