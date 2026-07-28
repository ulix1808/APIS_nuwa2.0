"""Normalización de chunk_text para búsqueda (ñ→n, sin acentos)."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

CHUNK_TEXT_NORMALIZED_KEY = "chunk_text_normalized"


def normalize_chunk_search_text(value: str) -> str:
    if not value:
        return ""
    nfd = unicodedata.normalize("NFD", value)
    no_acc = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^a-z0-9\s]", "", no_acc.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalized_text_from_chunk_obj(obj: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, val in obj.items():
        if key == CHUNK_TEXT_NORMALIZED_KEY:
            continue
        if isinstance(val, str) and val.strip():
            parts.append(normalize_chunk_search_text(val))
    return " ".join(parts)


def prepare_chunk_text_for_storage(raw: str) -> tuple[str, str]:
    """Devuelve (chunk_text JSON/string para guardar, chunk_text_normalized plano)."""
    text = raw.strip()
    if not text:
        return "", ""

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        norm = normalize_chunk_search_text(text)
        return text, norm

    if not isinstance(parsed, dict):
        norm = normalize_chunk_search_text(text)
        return text, norm

    clean = {k: v for k, v in parsed.items() if k != CHUNK_TEXT_NORMALIZED_KEY}
    norm = normalized_text_from_chunk_obj(clean)
    clean[CHUNK_TEXT_NORMALIZED_KEY] = norm
    return json.dumps(clean, ensure_ascii=False), norm
