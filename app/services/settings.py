import re
import zoneinfo
from functools import lru_cache

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


@lru_cache(maxsize=1)
def known_timezones() -> frozenset[str]:
    """The names a timezone setting may take."""
    return frozenset(zoneinfo.available_timezones())


# --- login sessions -----------------------------------------------------------

SESSION_EPOCH_SETTING = "session_epoch"
FIRST_SESSION_EPOCH = "1"


def get_session_epoch(db: Session) -> str:
    """The number every login session must carry to count as logged in.

    A whole number as text, "1" until it is first bumped. A stored value that isn't a
    whole number counts as "1", so a damaged setting can't lock everyone out for good.
    """
    stored = get_setting(db, SESSION_EPOCH_SETTING)
    try:
        return str(int(stored)) if stored else FIRST_SESSION_EPOCH
    except ValueError:
        return FIRST_SESSION_EPOCH


def bump_session_epoch(db: Session) -> str:
    """End every login session at once: from now on only a session stamped with the
    returned number counts as logged in. Used when the password changes or is reset."""
    epoch = str(int(get_session_epoch(db)) + 1)
    set_setting(db, SESSION_EPOCH_SETTING, epoch)
    return epoch


# --- the values Settings lets the user change ---------------------------------

STORE_REGION_SETTING = "store_region"
REFRESH_TIME_SETTING = "refresh_time"
TIMEZONE_SETTING = "timezone"

DEFAULT_STORE_REGION = "us"
DEFAULT_REFRESH_TIME = "04:00"
DEFAULT_TIMEZONE = "UTC"

# Plain character classes, not \d: that would also take digits from other scripts.
_REGION = re.compile(r"[a-z]{2}")
_REFRESH_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def parse_region(text: str) -> str | None:
    """The two-letter store region in `text`, lowercase, or None when it isn't one.

    Spaces around it are ignored and any case is taken ("GB" is "gb").
    """
    text = text.strip()
    if not text.isascii():
        return None
    region = text.lower()
    return region if _REGION.fullmatch(region) else None


def parse_refresh_time(text: str) -> str | None:
    """The refresh time in `text` as "HH:MM" on a 24-hour clock, or None."""
    time = text.strip()
    return time if _REFRESH_TIME.fullmatch(time) else None


def parse_timezone(text: str) -> str | None:
    """The timezone name in `text`, or None when it isn't one `known_timezones` lists."""
    name = text.strip()
    return name if name in known_timezones() else None
