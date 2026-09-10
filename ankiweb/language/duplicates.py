from __future__ import annotations

import html
from html.parser import HTMLParser
import re
import unicodedata

from ankiweb.language.card_types import CardTypeSpec


_BREAK_RE = re.compile(r"<(?:br|/p|/div)\s*/?>", re.IGNORECASE)
_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^{}]*?)?\}\}", re.IGNORECASE)
_SOUND_RE = re.compile(r"\[sound:[^\]]+\]", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_duplicate_value(value: str) -> str:
    """Normalize exact duplicate keys while keeping Spanish accents significant."""
    value = html.unescape(value)
    value = _CLOZE_RE.sub(r"\1", value)
    value = _SOUND_RE.sub(" ", value)
    parser = _TextExtractor()
    parser.feed(_BREAK_RE.sub(" ", value))
    parser.close()
    value = unicodedata.normalize("NFKC", " ".join(parser.parts))
    value = "".join(" " if unicodedata.category(char).startswith("P") else char for char in value)
    return " ".join(value.split()).casefold()


def duplicate_key(spec: CardTypeSpec, fields: dict[str, str]) -> tuple[str, str]:
    parts = [normalize_duplicate_value(fields.get(name, "")) for name in spec.duplicate_fields]
    return spec.duplicate_kind, "\x1f".join(parts)
