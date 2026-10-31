"""The prompts the app sends to a model: Jinja2 text files in this folder.

A prompt's version is the sha256 hex of its file's bytes, so any edit to a prompt gives it a
new version, and the call log says which wording produced each reply.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, StrictUndefined

PROMPTS_DIR = Path(__file__).parent

# Plain text goes to a model, not a web page, so nothing is escaped. A name the prompt uses
# but the caller left out is an error rather than a silent blank.
_ENVIRONMENT = Environment(
    undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
)


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    source: str

    def render(self, /, **context: object) -> str:
        return _ENVIRONMENT.from_string(self.source).render(**context)


def load_prompt(name: str) -> Prompt:
    """Read `{name}.md` from the prompts folder. Raises FileNotFoundError if it isn't there."""
    data = (PROMPTS_DIR / f"{name}.md").read_bytes()
    return Prompt(
        name=name,
        version=hashlib.sha256(data).hexdigest(),
        source=data.decode("utf-8"),
    )
