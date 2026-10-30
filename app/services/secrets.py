import os
import secrets
import tempfile
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet


def _write_private_file(path: Path, data: bytes) -> None:
    """Write `data` to `path` so a crash can't leave a half-written file behind.

    The data goes to a temp file in the same folder, then replaces `path` in one step.
    """
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass  # Windows only toggles read-only, and some mounts refuse chmod.
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_or_create_key(path: Path) -> bytes:
    """Return the Fernet key stored at `path`, creating the file on first use."""
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    _write_private_file(path, key)
    return key


def load_or_create_session_secret(path: Path) -> str:
    """Return the session signing secret stored at `path`, creating the file on first use."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(32)
    _write_private_file(path, secret.encode("utf-8"))
    return secret


class Vault:
    """Encrypts and decrypts short strings, such as API keys, with one Fernet key."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")


_hasher = PasswordHasher()


def hash_password(pw: str) -> str:
    return _hasher.hash(pw)


def verify_password(hashed: str, pw: str) -> bool:
    try:
        return _hasher.verify(hashed, pw)
    except VerifyMismatchError:
        return False


MIN_PASSWORD_LENGTH = 8

# Fixed copy: plain strings, escaped like any text when a template shows them.
PASSWORD_TOO_SHORT = f"Use at least {MIN_PASSWORD_LENGTH} characters."
PASSWORDS_DIFFER = "The two passwords don't match."


def new_password_problem(password: str, confirm: str) -> tuple[str, str] | None:
    """The first rule a new password breaks, or None when it is fine.

    The first-run page and the Settings password change share this. The answer is the
    field the message belongs under ("password" or "confirm") and the message, which says
    what to do.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return "password", PASSWORD_TOO_SHORT
    if confirm != password:
        return "confirm", PASSWORDS_DIFFER
    return None
