"""Normalización, match y validación de entidades PF/PM."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

ROLE_PF = frozenset(
    {
        "Accionista",
        "Apoderado",
        "Rep. Legal",
        "Consejero",
        "Beneficiario Final",
        "Comisario",
        "Otro",
    }
)
ROLE_PM = frozenset(
    {
        "Cliente",
        "Proveedor",
        "Contraparte",
        "Subsidiaria",
        "Matriz",
        "Otro",
    }
)

PARTY_TYPE_LABELS = {
    "individual": "Persona física",
    "organization": "Persona moral",
}


def normalize_name(s: str) -> str:
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s)
    no_acc = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^a-z0-9\s]", "", no_acc.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_identifier(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def party_type_label(party_type: str, explicit: str | None = None) -> str:
    if explicit and explicit in PARTY_TYPE_LABELS.values():
        return explicit
    return PARTY_TYPE_LABELS.get(party_type, party_type)


def validate_relationship_role(party_type: str, role: str | None) -> str | None:
    if not role:
        return None
    allowed = ROLE_PF if party_type == "individual" else ROLE_PM
    if role not in allowed:
        return f"relationshipRole '{role}' no válido para partyType={party_type}"
    return None


def build_display_name(
    *,
    party_type: str,
    first_name: str | None = None,
    last_name: str | None = None,
    legal_name: str | None = None,
    full_name: str | None = None,
) -> str:
    if party_type == "organization":
        return (legal_name or full_name or "").strip()
    parts = [p for p in [(first_name or "").strip(), (last_name or "").strip()] if p]
    if parts:
        return " ".join(parts)
    return (full_name or "").strip()


def parse_full_name_fields(full_name: str, party_type: str) -> dict[str, str | None]:
    t = full_name.strip()
    if not t:
        return {"full_name": None, "first_name": None, "last_name": None, "legal_name": None}
    if party_type == "organization":
        return {"full_name": t, "first_name": None, "last_name": None, "legal_name": t}
    bits = t.split()
    if len(bits) == 1:
        return {"full_name": t, "first_name": bits[0], "last_name": None, "legal_name": None}
    return {
        "full_name": t,
        "first_name": bits[0],
        "last_name": " ".join(bits[1:]),
        "legal_name": None,
    }


def classify_id_number(raw: str | None) -> tuple[str | None, str | None]:
    """Devuelve (rfc, curp) a partir de un identificador único."""
    n = normalize_identifier(raw)
    if not n:
        return None, None
    if len(n) == 18 and re.match(r"^[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]{2}$", n):
        return None, n
    if len(n) in (12, 13) and re.match(r"^[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}$", n):
        return n, None
    return n, None


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1))
            )
        prev = cur
    return prev[-1]


def name_similarity(a: str, b: str) -> int:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    dist = _levenshtein(na, nb)
    return round((1 - dist / max(len(na), len(nb))) * 100)


def _word_overlap_score(a: str, b: str) -> int:
    wa = [w for w in normalize_name(a).split() if len(w) > 2]
    wb = [w for w in normalize_name(b).split() if len(w) > 2]
    if not wa or not wb:
        return 0
    shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    matched = [w for w in shorter if w in longer]
    if not matched:
        return 0
    return round((len(matched) / len(shorter)) * 80)


def find_matches(
    *,
    party_type: str,
    legal_name: str | None,
    first_name: str | None,
    last_name: str | None,
    full_name: str | None,
    rfc: str | None,
    curp: str | None,
    candidates: list[dict[str, Any]],
    min_confidence: int = 60,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    search_rfc = normalize_identifier(rfc)
    search_curp = normalize_identifier(curp)
    if party_type == "organization":
        search_legal = normalize_name(legal_name or full_name or "")
    else:
        fn, ln = (first_name or "").strip(), (last_name or "").strip()
        if not fn and not ln and full_name:
            parsed = parse_full_name_fields(full_name, "individual")
            fn = parsed.get("first_name") or ""
            ln = parsed.get("last_name") or ""
        search_full = normalize_name(" ".join(p for p in [fn, ln] if p) or (full_name or ""))

    matches: list[dict[str, Any]] = []

    for c in candidates:
        cid = str(c["id"])
        c_rfc = normalize_identifier(c.get("rfc"))
        c_curp = normalize_identifier(c.get("curp"))
        c_name = c.get("name") or ""
        c_legal = normalize_name(c.get("legal_name") or "")
        c_first = (c.get("first_name") or "").strip()
        c_last = (c.get("last_name") or "").strip()
        c_full_norm = normalize_name(
            c_name
            or " ".join(p for p in [c_first, c_last] if p)
            or (c.get("full_name") or "")
            or (c.get("legal_name") or "")
        )

        if party_type == "organization":
            has_name = bool(search_legal)
            has_id = bool(search_rfc)
            name_ok = has_name and (search_legal == c_legal or name_similarity(search_legal, c_legal) >= 85)
            id_ok = has_id and search_rfc and search_rfc == c_rfc
            if has_name and has_id:
                if name_ok and id_ok:
                    matches.append(_match_row(c, "exact_identifier", 100, "Razón social y RFC coinciden"))
                elif id_ok:
                    matches.append(_match_row(c, "exact_identifier", 95, f"RFC coincide: {c.get('rfc')}"))
                elif name_ok:
                    sim = name_similarity(search_legal, c_legal or c_name)
                    matches.append(_match_row(c, "fuzzy_name", sim, f"Razón social similar ({sim}%)"))
            elif has_id and id_ok:
                matches.append(_match_row(c, "exact_identifier", 100, f"RFC coincide: {c.get('rfc')}"))
            elif has_name:
                sim = name_similarity(search_legal, c_legal or c_name)
                if sim >= 85:
                    matches.append(_match_row(c, "fuzzy_name", sim, f"Razón social similar ({sim}%)"))
                elif sim >= min_confidence:
                    matches.append(_match_row(c, "word_overlap", sim, f"Nombre similar ({sim}%)"))
        else:
            has_name = bool(search_full)
            has_id = bool(search_rfc or search_curp)
            name_ok = has_name and (
                search_full == c_full_norm or name_similarity(search_full, c_full_norm) >= 85
            )
            rfc_ok = bool(search_rfc and c_rfc and search_rfc == c_rfc)
            curp_ok = bool(search_curp and c_curp and search_curp == c_curp)
            id_ok = rfc_ok or curp_ok
            if has_name and has_id:
                if name_ok and id_ok:
                    detail = "Nombre e identificador coinciden"
                    matches.append(_match_row(c, "exact_identifier", 100, detail))
                elif id_ok:
                    ident = c.get("curp") if curp_ok else c.get("rfc")
                    matches.append(_match_row(c, "exact_identifier", 95, f"Identificador coincide: {ident}"))
                elif name_ok:
                    sim = name_similarity(search_full, c_full_norm)
                    matches.append(_match_row(c, "fuzzy_name", sim, f"Nombre similar ({sim}%)"))
            elif has_id and id_ok:
                ident = c.get("curp") if curp_ok else c.get("rfc")
                matches.append(_match_row(c, "exact_identifier", 100, f"Identificador coincide: {ident}"))
            elif has_name:
                sim = name_similarity(search_full, c_full_norm)
                if sim >= 85:
                    matches.append(_match_row(c, "fuzzy_name", sim, f"Nombre similar ({sim}%)"))
                else:
                    wo = _word_overlap_score(search_full, c_full_norm)
                    if wo >= min_confidence:
                        matches.append(_match_row(c, "word_overlap", wo, "Palabras coincidentes"))

    dedup: dict[str, dict[str, Any]] = {}
    for m in matches:
        eid = m["entityId"]
        if eid not in dedup or m["confidence"] > dedup[eid]["confidence"]:
            dedup[eid] = m
    out = sorted(dedup.values(), key=lambda x: x["confidence"], reverse=True)
    return out[:max_results]


def _match_row(c: dict[str, Any], match_type: str, confidence: int, details: str) -> dict[str, Any]:
    return {
        "entityId": str(c["id"]),
        "name": c.get("name"),
        "partyType": c.get("party_type"),
        "partyTypeLabel": c.get("party_type_label"),
        "rfc": c.get("rfc"),
        "curp": c.get("curp"),
        "country": c.get("country"),
        "riskLevel": c.get("risk_level"),
        "lastScreeningAt": c.get("last_screening_at"),
        "reportCount": int(c.get("report_count") or 0),
        "matchType": match_type,
        "confidence": confidence,
        "matchDetails": details,
    }


def has_strong_match(matches: list[dict[str, Any]]) -> bool:
    for m in matches:
        if m["matchType"] == "exact_identifier":
            return True
        if m["matchType"] == "exact_name" or (
            m["matchType"] == "fuzzy_name" and m["confidence"] >= 90
        ):
            return True
    return False


def risk_from_report(nivel: str | None, numerico: int | None) -> str | None:
    if numerico is not None:
        if numerico >= 3:
            return "critical"
        if numerico == 2:
            return "medium"
        if numerico <= 1:
            return "low"
    n = (nivel or "").lower()
    if n in ("critical", "high"):
        return "high" if n == "high" else "critical"
    if n in ("warning", "medium", "review"):
        return "medium"
    if n in ("clear", "low"):
        return "low"
    return None


def merge_risk(current: str | None, new: str | None) -> str | None:
    order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    if not new:
        return current
    if not current:
        return new
    return new if order.get(new, 0) > order.get(current, 0) else current
