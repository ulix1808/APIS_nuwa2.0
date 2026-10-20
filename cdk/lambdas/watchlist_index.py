"""
In-memory index of a list (OFAC records or SAT records) and the match decision of the early warning.

Decision for one subject (entity, partner or representative) against one list record:
1. An ID in common (RFC — with or without homoclave —, CURP, cédula, passport, …) → match, severity high.
2. IDs of the same kind that differ (two RFCs, two CURPs, a CURP and an RFC of other birth dates…) →
   different taxpayer or person (homonym): no match.
3. People (PF): only an ID proves it. The name alone never alerts. An RFC whose first 10 characters are the
   CURP's on the list, plus the full name, is a probable match (medium).
4. Companies (PM): every word of the registered name (without legal forms) in the list name or one of its
   aliases → high; one letter off → medium ("se propone revisar").
OFAC weak aliases (lowQuality) are not used to match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from watchlist_names import (
    IdSet,
    company_identity_key,
    compare_ids,
    is_searchable,
    match_tokens,
    name_tokens,
    rfc_base,
    _one_edit,
)

MATCHABLE_PARTY_TYPES = ("individual", "organization")


@dataclass
class ListRecord:
    """One record of a list, ready to match."""

    ref: str  # OFAC ext_id, or '<source_id>:<record key>' for SAT
    list_code: str  # 'ofac' | 'sat'
    party_type: str  # individual | organization | unknown
    name: str
    names: list[str]  # name + usable aliases
    ids: IdSet
    content_hash: str = ""
    info: dict[str, Any] = field(default_factory=dict)  # what the alert shows (lists, programs, dates…)


@dataclass
class Subject:
    """An entity of a client, or one of its partners / representatives."""

    key: str  # stable id of the subject inside its entity ('entity' or 'relation:<id>')
    name: str
    party_type: str  # individual | organization | unknown
    ids: IdSet
    aliases: list[str] = field(default_factory=list)


@dataclass
class Match:
    record: ListRecord
    matched_by: str  # rfc | curp | <id type> | rfc_curp | name | name_fuzzy
    severity: str  # high | medium
    matched_name: str
    name_level: str | None  # exact | fuzzy | None (ID only)


def _types_compatible(subject_type: str, record_type: str) -> bool:
    if subject_type == "unknown" or record_type == "unknown":
        return True
    return subject_type == record_type


def _best_name_level(subject: Subject, record: ListRecord, party_type: str) -> tuple[str | None, str]:
    best: tuple[str | None, str] = (None, "")
    for subject_name in [subject.name, *subject.aliases]:
        query = name_tokens(subject_name, party_type)
        if not is_searchable(query, party_type):
            continue
        for candidate in record.names:
            level = match_tokens(query, name_tokens(candidate, party_type))
            if level == "exact":
                return "exact", candidate
            if level == "fuzzy" and best[0] is None:
                best = ("fuzzy", candidate)
    return best


def evaluate(subject: Subject, record: ListRecord) -> Match | None:
    if not _types_compatible(subject.party_type, record.party_type):
        return None
    party_type = subject.party_type if subject.party_type != "unknown" else record.party_type
    if party_type == "unknown":
        party_type = "organization"
    verdict = compare_ids(subject.ids, record.ids)
    level, matched_name = _best_name_level(subject, record, party_type)
    if verdict.confirmed_by:
        return Match(record, verdict.confirmed_by, "high", matched_name or record.name, level)
    if verdict.conflict:
        return None
    if party_type == "individual":
        if verdict.weak_rfc_curp and level == "exact":
            return Match(record, "rfc_curp", "medium", matched_name, level)
        return None
    if level == "exact":
        return Match(record, "name", "high", matched_name, level)
    if level == "fuzzy":
        return Match(record, "name_fuzzy", "medium", matched_name, level)
    return None


class ListIndex:
    """Token, compact-name and ID postings over a set of list records."""

    def __init__(self, records: Iterable[ListRecord]) -> None:
        self.records: list[ListRecord] = []
        self._by_token: dict[str, set[int]] = {}
        self._by_compact: dict[str, set[int]] = {}
        self._by_id: dict[str, set[int]] = {}
        for rec in records:
            self.add(rec)

    def __len__(self) -> int:
        return len(self.records)

    def add(self, rec: ListRecord) -> None:
        i = len(self.records)
        self.records.append(rec)
        for name in rec.names:
            for party_type in self._party_types(rec.party_type):
                tokens = name_tokens(name, party_type)
                for t in tokens:
                    self._by_token.setdefault(t, set()).add(i)
                if tokens:
                    self._by_compact.setdefault("".join(tokens), set()).add(i)
        for value in self._id_keys(rec.ids):
            self._by_id.setdefault(value, set()).add(i)

    @staticmethod
    def _party_types(party_type: str) -> tuple[str, ...]:
        return MATCHABLE_PARTY_TYPES if party_type == "unknown" else (party_type,)

    @staticmethod
    def _id_keys(ids: IdSet) -> set[str]:
        keys: set[str] = set()
        for r in ids.rfc:
            keys.add(r)
            base = rfc_base(r)
            if base:
                keys.add(f"rfcbase:{base}")
                if len(base) == 10:
                    keys.add(f"pf10:{base}")
        for c in ids.curp:
            keys.add(c)
            keys.add(f"pf10:{c[:10]}")
        for _, v in ids.other:
            keys.add(v)
        return keys

    def _candidates(self, subject: Subject) -> set[int]:
        out: set[int] = set()
        for key in self._id_keys(subject.ids):
            out |= self._by_id.get(key, set())
        if subject.ids.other:
            for _, v in subject.ids.other:
                out |= self._by_id.get(v, set())
        for name in [subject.name, *subject.aliases]:
            for party_type in self._party_types(subject.party_type):
                query = name_tokens(name, party_type)
                if not is_searchable(query, party_type):
                    continue
                out |= self._by_compact.get("".join(query), set())
                counts: dict[int, int] = {}
                for t in set(query):
                    for i in self._by_token.get(t, ()):
                        counts[i] = counts.get(i, 0) + 1
                need = len(set(query)) - 1 if len(set(query)) > 1 else 1
                out |= {i for i, n in counts.items() if n >= need}
                if len(query) == 1:
                    # One-word company: the one-letter-off spellings.
                    token = query[0]
                    for other, ids in self._by_token.items():
                        if abs(len(other) - len(token)) <= 1 and _one_edit(token, other):
                            out |= ids
        return out

    def match(self, subject: Subject) -> list[Match]:
        found: list[Match] = []
        for i in sorted(self._candidates(subject)):
            m = evaluate(subject, self.records[i])
            if m is not None:
                found.append(m)
        found.sort(key=lambda m: (m.severity != "high", m.record.ref))
        return found


def ofac_record_from_row(row: dict[str, Any]) -> ListRecord:
    """watchlist_entries row (with details.ids) → ListRecord."""
    aliases = row.get("aliases") or []
    names = [row["name"]] + [a["name"] for a in aliases if not a.get("lowQuality") and a.get("name")]
    details = row.get("details") or {}
    ids = IdSet.build(other=[(d.get("type") or "", d.get("value") or "") for d in details.get("ids", [])])
    party_type = row["party_type"] if row["party_type"] in MATCHABLE_PARTY_TYPES else "other"
    return ListRecord(
        ref=str(row["ext_id"]),
        list_code="ofac",
        party_type=party_type,
        name=row["name"],
        names=names,
        ids=ids,
        content_hash=row.get("content_hash") or "",
        info={
            "lists": row.get("lists") or [],
            "programs": row.get("programs") or [],
            "designatedOn": str(row["designated_on"]) if row.get("designated_on") else None,
            "birthDates": row.get("birth_dates") or [],
            "nationalities": row.get("nationalities") or [],
            "countries": row.get("countries") or [],
            "linked": details.get("linked", []),
            "idsShown": [
                {"type": d.get("type"), "label": d.get("label"), "value": d.get("raw") or d.get("value"), "country": d.get("country")}
                for d in details.get("ids", [])
            ],
        },
    )


def subject_ids(rfc: str | None, curp: str | None, other_ids: Iterable[dict[str, Any]] | None = None) -> IdSet:
    """Entity fields → IdSet. other_ids: [{type, value}] from the 'Otro identificador' field."""
    others = [(str(d.get("type") or "other"), str(d.get("value") or "")) for d in (other_ids or []) if isinstance(d, dict)]
    return IdSet.build(rfc=[rfc] if rfc else [], curp=[curp] if curp else [], other=others)


def identity_key(name: str, party_type: str) -> str:
    """Stable name key of a subject (dedupe of partners across reports)."""
    if party_type == "organization":
        return company_identity_key(name)
    return " ".join(sorted(name_tokens(name, "individual")))
