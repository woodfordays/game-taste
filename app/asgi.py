import os
from pathlib import Path

from app.main import create_app

app = create_app(Path(os.environ.get("GAME_TASTE_DATA", "/data")))
