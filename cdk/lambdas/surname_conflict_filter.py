"""Exclude catalog hits for a person whose full name carries a different surname.

"Joel Hernández García" is another person when screening "Joel Hernández Hernández":
the hit lacks one of the subject's surnames AND has a name token the subject does not.
Hits that only omit tokens ("Joel Hernández", "Joel Hernández N") or spell the same name
differently stay, since they cannot be ruled out. Applies only to individuals and only
when the hit name comes from a structured field, never from free text.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from oic_responsable_filter import parse_chunk_fields

_LOGGER = logging.getLogger("nuwa.obs")

_PARTICLES = frozenset({"de", "del", "la", "las", "los", "y", "e", "da", "di", "van", "von"})
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

_NAME_KEYS = (
    "Nombre del Contribuyente",
    "Razón Social",
    "Razon Social",
    "Denominación",
    "Denominacion",
    "nombre",
    "Nombre",
)
_NAME_KEY_RE = re.compile(r"contribuyente|nombre|raz[oó]n|denominaci", re.IGNORECASE)
_NOT_SUBJECT_KEY_RE = re.compile(r"oic|responsable|autoridad|dependencia|notario|funcionario", re.IGNORECASE)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()


def person_name_tokens(name: str) -> list[str]:
    return [
        t
        for t in _TOKEN_SPLIT.split(_fold(name))
        if len(t) >= 2 and t not in _PARTICLES and not t.isdigit()
    ]


def _same_token(a: str, b: str) -> bool:
    """Equal, or one edit apart for tokens of 5+ letters ("hernandes" ~ "hernandez").

    The first letter must match: "fernandez" is a different surname, not a typo.
    """
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1 or a[0] != b[0]:
        return False
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) > len(b):
            i += 1
        elif len(b) > len(a):
            j += 1
        else:
            i += 1
            j += 1
    return edits + (len(a) - i) + (len(b) - j) <= 1


def _unmatched(source: list[str], against: list[str]) -> list[str]:
    """Count-aware difference: tokens of `source` not matched by a distinct token of `against`."""
    pool = list(against)
    out: list[str] = []
    for t in source:
        k = next((idx for idx, p in enumerate(pool) if _same_token(p, t)), -1)
        if k >= 0:
            pool.pop(k)
        else:
            out.append(t)
    return out


def has_surname_conflict(subject_name: str, hit_name: str, last_name: str = "") -> bool:
    subject = person_name_tokens(subject_name)
    hit = person_name_tokens(hit_name)
    if len(subject) < 3 or len(hit) < 2:
        return False
    from_form = person_name_tokens(last_name)
    surnames = from_form if from_form else subject[-2:]
    missing = _unmatched(subject, hit)
    missing_surnames = _unmatched(surnames, _unmatched(subject, missing))
    extra = _unmatched(hit, subject)
    return bool(missing_surnames) and bool(extra)


def extract_hit_name(chunk_text: str) -> str | None:
    fields = parse_chunk_fields(chunk_text)
    if not fields:
        return None
    for key in _NAME_KEYS:
        val = fields.get(key)
        if val and val.strip():
            return val.strip()
    for key, val in fields.items():
        if val.strip() and _NAME_KEY_RE.search(key) and not _NOT_SUBJECT_KEY_RE.search(key):
            return val.strip()
    return None


def filter_surname_conflicts(
    rows: list[dict[str, Any]],
    *,
    query: str,
    last_name: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """Drop person hits with a different surname. Returns (kept, excluded_count)."""
    if len(person_name_tokens(query)) < 3 or not rows:
        return rows, 0

    kept: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        hit_name = extract_hit_name(str(row.get("chunk_text") or row.get("chunkText") or ""))
        if hit_name and has_surname_conflict(query, hit_name, last_name):
            excluded += 1
            _LOGGER.info(
                "search_exclude reason=surname_conflict query=%s hit=%s chunk_id=%s",
                query[:80],
                hit_name[:120],
                row.get("id") or row.get("chunk_id") or "",
            )
            continue
        kept.append(row)
    return kept, excluded
