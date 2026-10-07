"""Exclude catalog hits that match only on OIC Responsable (not the sanctioned subject).

Applies only to records that expose an "OIC Responsable" (or equivalent) field.
Other sources / unstructured chunks are left untouched.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from chunk_normalize import normalize_chunk_search_text

_LOGGER = logging.getLogger("nuwa.obs")

# Subject / sanctioned-party field names (case-folded).
_SUBJECT_KEYS = frozenset(
    {
        "nombre",
        "name",
        "razon social",
        "razón social",
        "razon_social",
        "nombre del proveedor",
        "nombre del licitante",
        "nombre del contratista",
        "proveedor",
        "licitante",
        "contratista",
        "sancionado",
        "nombre sancionado",
    }
)

# OIC / authority field names (case-folded).
_OIC_KEYS = frozenset(
    {
        "oic responsable",
        "oic_responsable",
        "oicresponsable",
        "organo interno de control",
        "órgano interno de control",
        "organismo responsable",
        "autoridad responsable",
        "dependencia responsable",
    }
)

_KV_SPLIT = re.compile(r"\s*[·|]\s*")
_KV_PAIR = re.compile(r"^([^:]+):\s*(.*)$")


def _fold_key(key: str) -> str:
    return normalize_chunk_search_text(key.replace("_", " "))


def _field_contains_query(field: str, query: str) -> bool:
    """Conservative containment: normalized query phrase inside normalized field."""
    nq = normalize_chunk_search_text(query)
    nf = normalize_chunk_search_text(field)
    if not nq or not nf:
        return False
    if nq in nf:
        return True
    # Token fallback: every query token (len>=2) must appear in the field.
    tokens = [t for t in nq.split() if len(t) >= 2]
    if len(tokens) < 2:
        return False
    return all(t in nf for t in tokens)


def parse_chunk_fields(chunk_text: str) -> dict[str, str] | None:
    """Parse JSON object or 'key: value · key: value' chunk into a string map."""
    text = (chunk_text or "").strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        out: dict[str, str] = {}
        for k, v in parsed.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = str(v)
        return out or None

    # Plain "nombre: X · OIC Responsable: Y · ..."
    if ":" not in text:
        return None
    parts = _KV_SPLIT.split(text)
    out = {}
    for part in parts:
        m = _KV_PAIR.match(part.strip())
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if key and val:
            out[key] = val
    return out or None


def extract_subject_and_oic(fields: Mapping[str, str]) -> tuple[str | None, str | None]:
    subject: str | None = None
    oic: str | None = None
    for key, val in fields.items():
        folded = _fold_key(key)
        if folded in _OIC_KEYS and val.strip():
            oic = val.strip()
        elif folded in _SUBJECT_KEYS and val.strip() and subject is None:
            subject = val.strip()
    # Prefer explicit "nombre" when multiple subject-like keys exist.
    for key, val in fields.items():
        if _fold_key(key) == "nombre" and val.strip():
            subject = val.strip()
            break
    return subject, oic


def should_exclude_oic_only_match(
    *,
    query: str,
    chunk_text: str,
) -> tuple[bool, dict[str, Any] | None]:
    """
    Return (exclude, debug_payload).

    Exclude when the record has OIC Responsable, the query matches that field,
    and the query does NOT match the sanctioned subject name.
    """
    q = (query or "").strip()
    if not q:
        return False, None

    fields = parse_chunk_fields(chunk_text)
    if not fields:
        return False, None

    subject, oic = extract_subject_and_oic(fields)
    if not oic:
        # No OIC field → rule does not apply (other sources / unstructured).
        return False, None

    match_oic = _field_contains_query(oic, q)
    match_subject = bool(subject) and _field_contains_query(subject, q)

    if match_oic and not match_subject:
        payload = {
            "excluded": True,
            "reason": "match_only_on_oic_responsable",
            "query": q,
            "subject_name": subject or "",
            "oic_responsable": oic,
        }
        return True, payload

    return False, None


def filter_oic_only_hits(
    rows: list[dict[str, Any]],
    *,
    query: str,
) -> tuple[list[dict[str, Any]], int]:
    """Drop rows whose match is only on OIC Responsable. Returns (kept, excluded_count)."""
    if not (query or "").strip() or not rows:
        return rows, 0

    kept: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        chunk = str(row.get("chunk_text") or row.get("chunkText") or "")
        drop, payload = should_exclude_oic_only_match(query=query, chunk_text=chunk)
        if drop:
            excluded += 1
            if payload:
                _LOGGER.info(
                    "search_exclude reason=%s query=%s subject=%s oic=%s chunk_id=%s",
                    payload.get("reason"),
                    payload.get("query"),
                    (payload.get("subject_name") or "")[:120],
                    (payload.get("oic_responsable") or "")[:120],
                    row.get("chunk_id") or row.get("chunkId") or "",
                )
            continue
        kept.append(row)
    return kept, excluded
