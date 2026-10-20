"""
Name and ID rules of the early-warning list match (same rules as the BFF entity matcher).

Names:
- Organizations (PM) compare without legal forms anywhere in the name ("S.A. de C.V.", "S.P.R. de R.L.",
  "Institución de Banca Múltiple", glued "SA DE CV") and without connectors, so the entity "Green
  Agropacific" is "GREEN AGROPACIFIC S.P.R. DE R.L. DE C.V.". Port of companyIdentityKey
  (b_CvPklPEoxLx/lib/entity-aliases.ts).
- People (PF) compare by name tokens in any order ("Liliana Cisneros Tapia" = "CISNEROS TAPIA, Liliana").
- Every token of the searched name must be in the list name (each one once). The list name may have more
  words. One letter of difference (one edit in one token of 5+ letters) is a probable match, not exact.

IDs:
- Values compare upper case, letters and digits only.
- A person's RFC without homoclave (10 chars) matches the full RFC (13); the same for companies (9 / 12).
- A CURP and an RFC of the same person share their first 10 characters; that alone is not proof (two
  people can share initials and birth date), so it only confirms when the name also matches.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

# Trailing legal forms, longest first ("Control" keeps its "co": only whole trailing words).
_LEGAL_SUFFIXES = (
    "s p r de r l de c v",
    "s p r de r i de c v",
    "s p r de r l",
    "s p r de r i",
    "s a p i b de c v",
    "s a p i de c v",
    "s a b de c v",
    "s de r l de c v",
    "s a de c v",
    "s en c por a",
    "s en c",
    "s a p i b",
    "s a p i",
    "s a b",
    "s a s",
    "s a c",
    "s a a",
    "s a",
    "s c s",
    "s c l",
    "s de r l",
    "s p r",
    "s r l",
    "e i r l",
    "de c v",
    "s c",
    "a c",
    "c v",
    "ltda",
    "limitada",
    "spa",
    "inc",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "company",
    "co",
)

_GLUED_LEGAL_TOKENS = {
    "sapib": "s a p i b",
    "sapi": "s a p i",
    "spr": "s p r",
    "sab": "s a b",
    "sas": "s a s",
    "sac": "s a c",
    "saa": "s a a",
    "scs": "s c s",
    "scl": "s c l",
    "srl": "s r l",
    "eirl": "e i r l",
    "ltda": "ltda",
    "limitada": "limitada",
    "sa": "s a",
    "sc": "s c",
    "ac": "a c",
    "cv": "c v",
    "rl": "r l",
}

_INTERIOR_LEGAL_FORMS = (
    "sociedad anonima promotora de inversion de capital variable",
    "sociedad anonima bursatil de capital variable",
    "sociedad anonima de capital variable",
    "promotora de inversion de capital variable",
    "sociedad de responsabilidad limitada",
    "sociedad de produccion rural",
    "institucion de banca multiple",
    "institution de banca multiple",
    "sociedad financiera de objeto multiple",
    "entidad no regulada",
    "sociedad anonima",
    "sociedad civil",
    "asociacion civil",
    "de capital variable",
    "s a p i b de c v",
    "s a p i de c v",
    "s a b de c v",
    "s de r l de c v",
    "s p r de r l de c v",
    "s a de c v",
    "s p r de r l",
    "s de r l",
    "s a p i",
    "s a b",
    "i b m",
    "sofom",
    "e n r",
    "s a",
    "s c",
    "a c",
)

_CONNECTORS = frozenset({"y", "e", "and", "de", "del", "la", "las", "los", "el", "the", "&"})

FUZZY_MIN_TOKEN_LEN = 5


def normalize_text(value: str) -> str:
    """Accent-free, lower case, letters/digits and single spaces ('Ñandú, S.A.' → 'nandu s a')."""
    nfd = unicodedata.normalize("NFD", value or "")
    plain = "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", plain)).strip()


def _expand_trailing_legal_tokens(normalized: str) -> str:
    tokens = normalized.split()
    index = len(tokens) - 1
    tail: list[str] = []
    while index >= 0:
        token = tokens[index]
        if token in ("de", "en", "por"):
            if not tail:
                break
            tail.insert(0, token)
            index -= 1
            continue
        expanded = _GLUED_LEGAL_TOKENS.get(token)
        if not expanded:
            break
        tail.insert(0, expanded)
        index -= 1
    if not tail:
        return normalized
    return " ".join(tokens[: index + 1] + tail)


def strip_legal_suffix(normalized: str) -> str:
    current = _expand_trailing_legal_tokens(normalized)
    changed = True
    while changed and current:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if current == suffix or not current.endswith(" " + suffix):
                continue
            current = current[: -(len(suffix) + 1)].strip()
            changed = True
            break
    return current


def company_tokens(name: str) -> list[str]:
    """Identity words of a company: no legal forms (anywhere) and no connectors."""
    normalized = normalize_text(name)
    text = f" {strip_legal_suffix(normalized) or normalized} "
    for form in _INTERIOR_LEGAL_FORMS:
        text = text.replace(f" {form} ", " ")
    return [w for w in text.split() if w not in _CONNECTORS]


def person_tokens(name: str) -> list[str]:
    return [w for w in normalize_text(name).split() if w not in _CONNECTORS]


def name_tokens(name: str, party_type: str) -> list[str]:
    return company_tokens(name) if party_type == "organization" else person_tokens(name)


def company_identity_key(name: str) -> str:
    return "".join(company_tokens(name))


def _one_edit(a: str, b: str) -> bool:
    """True if a and b differ by exactly one insertion, deletion or substitution."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    edited = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if la == lb:
            i += 1
        j += 1
    return True


def is_searchable(tokens: list[str], party_type: str) -> bool:
    """A lone short word ('Bajio', 'Juan') matches too much. People need name and surname."""
    if not tokens:
        return False
    if party_type == "organization":
        return len(tokens) >= 2 or len(tokens[0]) >= 6
    return len(tokens) >= 2


MIN_COVERAGE = 0.5


def match_tokens(query: list[str], candidate: list[str], *, min_coverage: float = MIN_COVERAGE) -> str | None:
    """
    'exact': every query token is in the candidate (each once, any order).
    'fuzzy': the same with one token one letter off (tokens of 5+ letters).
    None otherwise. Compact forms also count as exact ('Mefra Fletes' = 'Mefrafletes').
    The query must cover at least min_coverage of the candidate's words: two generic words ("Servicios
    Integrales") do not match inside a long name ("Resguardo de Valores y Servicios Integrales RSVI").
    """
    if not query or not candidate:
        return None
    if "".join(query) == "".join(candidate):
        return "exact"
    if len(query) < len(candidate) * min_coverage:
        return None
    pool = list(candidate)
    missing: list[str] = []
    for token in query:
        if token in pool:
            pool.remove(token)
        else:
            missing.append(token)
    if not missing:
        return "exact"
    if len(missing) > 1:
        return None
    token = missing[0]
    if len(token) < FUZZY_MIN_TOKEN_LEN:
        return None
    for other in pool:
        if len(other) >= FUZZY_MIN_TOKEN_LEN - 1 and _one_edit(token, other):
            return "fuzzy"
    return None


def normalize_id(value: str) -> str:
    nfd = unicodedata.normalize("NFD", value or "")
    plain = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", plain.upper())


_RFC_RE = re.compile(r"^[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{0,3}$")
_CURP_RE = re.compile(r"^[A-Z]{4}\d{6}[HMX][A-Z]{5}[A-Z0-9]\d$")


def looks_like_rfc(value: str) -> bool:
    return bool(_RFC_RE.match(value))


def looks_like_curp(value: str) -> bool:
    return bool(_CURP_RE.match(value))


def rfc_base(value: str) -> str:
    """RFC without homoclave: 10 chars for people (4 letters), 9 for companies (3 letters)."""
    if not looks_like_rfc(value):
        return ""
    letters = 4 if value[3].isalpha() else 3
    return value[: letters + 6]


def same_rfc(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    base_a, base_b = rfc_base(a), rfc_base(b)
    if not base_a or base_a != base_b:
        return False
    # Equal bases and one side without homoclave: same taxpayer as far as the data tells.
    return len(a) == len(base_a) or len(b) == len(base_b)


@dataclass
class IdSet:
    """IDs of one subject or one list record, normalized. other: {(type, value)}."""

    rfc: set[str] = field(default_factory=set)
    curp: set[str] = field(default_factory=set)
    other: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def build(cls, rfc: Iterable[str] = (), curp: Iterable[str] = (), other: Iterable[tuple[str, str]] = ()) -> "IdSet":
        out = cls()
        for v in rfc:
            n = normalize_id(v)
            if n:
                out.rfc.add(n)
        for v in curp:
            n = normalize_id(v)
            if n:
                out.curp.add(n)
        for t, v in other:
            n = normalize_id(v)
            if not n:
                continue
            # A Mexican tax ID written as "Tax ID No." is an RFC.
            if t == "rfc" or (t == "tax_id" and looks_like_rfc(n)):
                out.rfc.add(n)
            elif t == "curp" or looks_like_curp(n):
                out.curp.add(n)
            else:
                out.other.add((t, n))
        return out

    def empty(self) -> bool:
        return not (self.rfc or self.curp or self.other)


@dataclass
class IdVerdict:
    """confirmed_by: 'rfc' | 'curp' | '<type>' when an ID proves it; conflict when IDs of one kind differ."""

    confirmed_by: str | None = None
    conflict: bool = False
    weak_rfc_curp: bool = False  # RFC base = CURP prefix: only proof together with the name


def compare_ids(subject: IdSet, record: IdSet) -> IdVerdict:
    for a in subject.rfc:
        for b in record.rfc:
            if same_rfc(a, b):
                return IdVerdict(confirmed_by="rfc")
    if subject.curp & record.curp:
        return IdVerdict(confirmed_by="curp")
    for t, v in subject.other:
        if (t, v) in record.other:
            return IdVerdict(confirmed_by=t)
    # Values typed by the user without a type ("Otro identificador") may be any record ID.
    other_values = {v for _, v in record.other}
    for t, v in subject.other:
        if t == "other" and v in other_values and len(v) >= 6:
            return IdVerdict(confirmed_by="id")

    verdict = IdVerdict()
    if subject.rfc and record.rfc:
        verdict.conflict = True
    if subject.curp and record.curp:
        verdict.conflict = True
    # Same document type, different number (two passports of different people, two cédulas…).
    subject_types = {t for t, _ in subject.other if t != "other"}
    record_types = {t for t, _ in record.other}
    if subject_types & record_types:
        verdict.conflict = True
    def person_rfc_bases(values: set[str]) -> set[str]:
        return {b for b in (rfc_base(r) for r in values) if len(b) == 10}

    for curps, rfcs in ((subject.curp, record.rfc), (record.curp, subject.rfc)):
        bases = person_rfc_bases(rfcs)
        if not curps or not bases:
            continue
        if {c[:10] for c in curps} & bases:
            verdict.weak_rfc_curp = True
        else:
            # A person's CURP and RFC always share birth date and initials: different → different people.
            verdict.conflict = True
    return verdict
