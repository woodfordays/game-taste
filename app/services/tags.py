"""Tags: the short labels on a review, kept as lowercase words joined by underscores."""

import re

_WHITESPACE = re.compile(r"\s+")
_LEADING_HASHES = re.compile(r"^[#\s]+")


def normalize_tags(raw: str) -> list[str]:
    """The tags in `raw`, a comma-separated list typed by the user.

    Each piece is trimmed, loses any leading `#` (and any space around them, so
    "# deck" is "deck"), is lowercased, and has every run of whitespace turned into one
    `_`. Pieces that end up empty are dropped, and so are repeats: the first one stays
    where it was typed.
    """
    tags: dict[str, None] = {}  # a dict keeps first-seen order
    for piece in raw.split(","):
        tag = _WHITESPACE.sub("_", _LEADING_HASHES.sub("", piece).strip().lower())
        if tag:
            tags.setdefault(tag)
    return list(tags)
