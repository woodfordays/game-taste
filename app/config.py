from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Locations of the files that live in the data folder."""

    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "game-taste.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def session_secret_path(self) -> Path:
        return self.data_dir / "session.secret"

    @property
    def reset_flag_path(self) -> Path:
        return self.data_dir / "reset-password"
