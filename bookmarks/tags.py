"""Tag normalization: tags are lowercase ASCII words joined by '-', '_' or '.'."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

MAX_TAG_LENGTH = 64

# Applied before ASCII folding so common names survive ("C++" -> "cpp", "R&S" -> "r-and-s").
_REPLACEMENTS = (("++", "pp"), ("#", "sharp"), ("&", "-and-"), ("đ", "d"))
_INVALID = re.compile(r"[^a-z0-9._-]+")
_SEPARATOR_RUN = re.compile(r"[._-]{2,}")


def normalize_tag(raw: str) -> str:
    """Return the canonical form of ``raw``, or "" if nothing usable remains."""
    text = raw.strip().lower()
    for old, new in _REPLACEMENTS:
        text = text.replace(old, new)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _INVALID.sub("-", text)
    text = _SEPARATOR_RUN.sub("-", text)
    return text.strip("._-")[:MAX_TAG_LENGTH].strip("._-")


def normalize_tags(raw_tags: Iterable[str]) -> list[str]:
    """Normalize, drop empties, de-duplicate and sort."""
    return sorted({tag for tag in map(normalize_tag, raw_tags) if tag})
