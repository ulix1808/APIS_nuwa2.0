"""Utilidades para documentos: MIME, keys S3, chunks de indexación."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

DEFAULT_MAX_BYTES = 50 * 1024 * 1024

# Fuentes/chunks auto-creados por documents/finalize: sin riesgo de lista (solo contexto KYC).
DOCUMENT_SOURCE_RISK_LEVEL = 0
DOCUMENT_SOURCE_CATEGORY_SLUG = "documento_interno"

ALLOWED_MIME_PREFIXES = (
    "application/pdf",
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/",
)


def max_upload_bytes() -> int:
    raw = os.environ.get("NUWA_DOCUMENTS_MAX_BYTES", "")
    try:
        return int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError:
        return DEFAULT_MAX_BYTES


def presign_ttl_seconds() -> int:
    raw = os.environ.get("NUWA_DOCUMENTS_PRESIGN_TTL", "900")
    try:
        return max(60, min(int(raw), 3600))
    except ValueError:
        return 900


def client_s3_prefix(client_id: int) -> str:
    return f"clients/{client_id}/"


def document_s3_key(client_id: int, document_id: str, original_filename: str) -> str:
    safe = sanitize_filename(original_filename)
    return f"clients/{client_id}/documents/{document_id}/{safe}"


def keep_marker_key(client_id: int) -> str:
    return f"clients/{client_id}/.keep"


def sanitize_filename(name: str) -> str:
    base = (name or "document").strip().replace("\\", "/").split("/")[-1]
    base = unicodedata.normalize("NFKD", base)
    base = "".join(c for c in base if c.isalnum() or c in "._- ")
    base = re.sub(r"\s+", "_", base).strip("._") or "document"
    return base[:200]


def mime_allowed(mime: str | None) -> bool:
    if not mime:
        return False
    m = mime.lower().split(";")[0].strip()
    return any(m == p or (p.endswith("/") and m.startswith(p)) for p in ALLOWED_MIME_PREFIXES)


def build_index_chunks(extracted: dict[str, Any]) -> list[str]:
    """Textos para risk_entity_chunks (orden estable)."""
    chunks: list[str] = []
    doc_type = extracted.get("documentType") or extracted.get("document_type")
    summary = extracted.get("summary") or ""
    if doc_type or summary:
        chunks.append(json.dumps({"documentType": doc_type, "summary": summary}, ensure_ascii=False))

    for party in extracted.get("parties") or []:
        if not isinstance(party, dict):
            continue
        chunks.append(json.dumps(party, ensure_ascii=False))

    for addr in extracted.get("addresses") or []:
        if isinstance(addr, dict):
            chunks.append(json.dumps({"type": "address", **addr}, ensure_ascii=False))

    for ident in extracted.get("identifiers") or []:
        if isinstance(ident, dict):
            chunks.append(json.dumps({"type": "identifier", **ident}, ensure_ascii=False))

    return [c for c in chunks if c.strip()]


def document_source_metadata(
    *,
    document_id: str,
    client_id: int,
    primary_entity_id: str | None,
    document_type: Any,
) -> dict[str, Any]:
    """Metadata de sources privadas creadas por documents/finalize (autoIndex)."""
    meta: dict[str, Any] = {
        "sourceOrigin": "document",
        "sourceKind": "document",
        "documentId": document_id,
        "clientId": client_id,
    }
    if primary_entity_id:
        meta["primaryEntityId"] = primary_entity_id
    if document_type is not None:
        meta["documentType"] = document_type
    return meta


def is_document_internal_source_metadata(metadata: Any) -> bool:
    """True si la fila sources proviene del módulo de documentos internos."""
    if not isinstance(metadata, dict):
        return False
    if metadata.get("sourceOrigin") == "document":
        return True
    return bool(metadata.get("documentId"))
