"""
OFAC sanctions lists (Treasury) in the Advanced XML format: SDN_ADVANCED.XML and CONS_ADVANCED.XML.

Both files carry every record of the CSV exports (SDN/ADD/ALT and CONS_PRIM/ADD/ALT) plus what the CSV
only has as free text: identity documents by type and country, the designation date, programs, birth
dates, nationalities and the "Linked To" relations. parse_advanced_xml() streams one file into plain
records; merge_records() joins the two files (93 parties are on both, same FixedRef).

Record shape (all values JSON-safe; content_hash covers everything but the hash itself):
  extId, partyType (individual | organization | vessel | aircraft | other), name,
  aliases [{name, type: aka|fka|nka, lowQuality}], lists, programs, designatedOn (YYYY-MM-DD),
  ids [{type, label, value, country}], birthDates, nationalities, countries, addresses,
  linked [{extId, relation, direction: to|from}], features {label: [values]}
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import IO, Any, Iterable

OFAC_EXPORTS_BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports"
OFAC_FILES = ("SDN_ADVANCED.XML", "CONS_ADVANCED.XML")

LATIN_SCRIPT_ID = "215"

# Name part types → order inside a person's name. Unknown types go last.
_PERSON_PART_ORDER = {
    "First Name": 0,
    "Middle Name": 1,
    "Last Name": 2,
    "Patronymic": 2,
    "Matronymic": 3,
    "Maiden Name": 4,
}

_ALIAS_TYPES = {"A.K.A.": "aka", "F.K.A.": "fka", "N.K.A.": "nka"}

# OFAC document labels → our ID types. Anything else keeps a slug of its label.
_ID_TYPES = {
    "R.F.C.": "rfc",
    "RFC": "rfc",
    "C.U.R.P.": "curp",
    "Cedula No.": "cedula",
    "Passport": "passport",
    "Diplomatic Passport": "passport",
    "British National Overseas Passport": "passport",
    "Stateless Person Passport": "passport",
    "National ID No.": "national_id",
    "Personal ID Card": "national_id",
    "Federal ID Card": "national_id",
    "Numero de Identidad": "national_id",
    "Tarjeta de Identidad": "national_id",
    "D.N.I.": "national_id",
    "Citizen's Card Number": "national_id",
    "Turkish Identification Number": "national_id",
    "Kenyan ID No.": "national_id",
    "Bosnian Personal ID No.": "national_id",
    "Moroccan Personal ID No.": "national_id",
    "Tazkira National ID Card": "national_id",
    "UAE Identification": "national_id",
    "CNP (Personal Numerical Code)": "national_id",
    "Tax ID No.": "tax_id",
    "NIT #": "tax_id",
    "RUC #": "tax_id",
    "RIF #": "tax_id",
    "C.U.I.T.": "tax_id",
    "C.U.I.": "tax_id",
    "RTN": "tax_id",
    "N.I.F.": "tax_id",
    "C.I.F.": "tax_id",
    "V.A.T. Number": "tax_id",
    "US FEIN": "tax_id",
    "Italian Fiscal Code": "tax_id",
    "Fiscal Code": "tax_id",
    "Paraguayan tax identification number": "tax_id",
    "Numero Unico de Identificacao Tributaria (NUIT)": "tax_id",
    "Romanian Tax Registration": "tax_id",
    "SSN": "ssn",
    "Folio Mercantil No.": "folio_mercantil",
    "Credencial electoral": "voter_id",
    "I.F.E.": "voter_id",
    "Electoral Registry No.": "voter_id",
    "Driver's License No.": "drivers_license",
    "Unified Social Credit Code (USCC)": "uscc",
    "United Social Credit Code Certificate (USCCC)": "uscc",
}

# Features whose value is a country (through a Location).
_COUNTRY_FEATURES = {"Nationality Country", "Citizenship Country", "Nationality of Registration"}


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _tag(c) == name]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _tag(c) == name:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _slug(label: str) -> str:
    nfd = unicodedata.normalize("NFD", label)
    plain = "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_") or "other"


def id_type_of(label: str) -> str:
    return _ID_TYPES.get(label.strip()) or _slug(label)


def normalize_id_value(value: str) -> str:
    """Upper case letters and digits only: 'CITL-801109 mmnspl07' → 'CITL801109MMNSPL07'."""
    nfd = unicodedata.normalize("NFD", value or "")
    plain = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", plain.upper())


def _ymd(el: ET.Element | None) -> str:
    """<Date>/<From> with Year, Month, Day → 'YYYY-MM-DD' (or 'YYYY-MM' / 'YYYY')."""
    if el is None:
        return ""
    y, m, d = _text(_child(el, "Year")), _text(_child(el, "Month")), _text(_child(el, "Day"))
    if not y:
        return ""
    if not m:
        return y
    if not d:
        return f"{y}-{int(m):02d}"
    return f"{y}-{int(m):02d}-{int(d):02d}"


def _date_period(el: ET.Element | None) -> str:
    """Birth date or similar. A range (circa 1960-1962) keeps both ends."""
    if el is None:
        return ""
    start, end = _child(el, "Start"), _child(el, "End")
    lo = _ymd(_child(start, "From")) if start is not None else ""
    hi = _ymd(_child(end, "To")) if end is not None else ""
    if lo and hi and lo != hi:
        return f"{lo}/{hi}"
    return lo or hi


class _Refs:
    """Reference value sets (ID → label) read at the top of the file."""

    def __init__(self) -> None:
        self.sets: dict[str, dict[str, Any]] = {}

    def get(self, set_name: str, ref_id: str | None) -> str:
        if not ref_id:
            return ""
        value = self.sets.get(set_name, {}).get(ref_id)
        return value if isinstance(value, str) else ""

    def add(self, el: ET.Element) -> None:
        name = _tag(el)
        bucket = self.sets.setdefault(name, {})
        ref_id = el.get("ID")
        if not ref_id:
            return
        if name == "PartySubType":
            bucket[ref_id] = el.get("PartyTypeID") or ""
            self.sets.setdefault("PartySubTypeName", {})[ref_id] = _text(el)
        else:
            bucket[ref_id] = _text(el)


def _party_type(refs: _Refs, subtype_id: str | None) -> str:
    ptype = refs.get("PartyType", refs.get("PartySubType", subtype_id))
    if ptype == "Individual":
        return "individual"
    if ptype == "Entity":
        return "organization"
    if ptype == "Transport":
        sub = refs.get("PartySubTypeName", subtype_id).lower()
        return sub if sub in ("vessel", "aircraft") else "other"
    return "other"


def _documented_name(refs: _Refs, name_el: ET.Element, part_types: dict[str, str], party_type: str) -> str | None:
    """Latin-script name as one string, people as 'First Middle Last'. None for other scripts."""
    parts: list[tuple[int, int, str]] = []
    for i, part in enumerate(_children(name_el, "DocumentedNamePart")):
        value = _child(part, "NamePartValue")
        if value is None or value.get("ScriptID") != LATIN_SCRIPT_ID:
            return None
        text = _text(value)
        if not text:
            continue
        ptype = part_types.get(value.get("NamePartGroupID") or "", "")
        order = _PERSON_PART_ORDER.get(ptype, 9) if party_type == "individual" else 0
        parts.append((order, i, text))
    if not parts:
        return None
    parts.sort()
    return re.sub(r"\s+", " ", " ".join(p[2] for p in parts)).strip()


def _location_info(loc: ET.Element, refs: _Refs) -> dict[str, str]:
    country = ""
    lc = _child(loc, "LocationCountry")
    if lc is not None:
        country = refs.get("Country", lc.get("CountryID"))
    parts: list[str] = []
    for lp in _children(loc, "LocationPart"):
        ptype = refs.get("LocPartType", lp.get("LocPartTypeID"))
        for v in _children(lp, "LocationPartValue"):
            text = _text(_child(v, "Value"))
            if not text:
                continue
            if ptype == "Unknown" and not country:
                # A bare "Mexico" location (nationality) has no LocationCountry.
                country = text
            else:
                parts.append(text)
    return {"country": country, "address": ", ".join(parts)}


def parse_advanced_xml(stream: IO[bytes] | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Streams one Advanced XML file. Returns (meta, records):
    meta = {"dateOfIssue": "YYYY-MM-DD", "parties": n}; records sorted by extId.
    """
    refs = _Refs()
    in_refs = False
    meta: dict[str, Any] = {}
    locations: dict[str, dict[str, str]] = {}
    docs_by_identity: dict[str, list[dict[str, str]]] = {}
    parties: dict[str, dict[str, Any]] = {}
    identity_to_party: dict[str, str] = {}
    profile_to_party: dict[str, str] = {}
    relations: list[tuple[str, str, str]] = []

    for event, el in ET.iterparse(stream, events=("start", "end")):
        tag = _tag(el)
        if event == "start":
            if tag == "ReferenceValueSets":
                in_refs = True
            continue
        if tag == "ReferenceValueSets":
            in_refs = False
            continue
        if in_refs:
            if el.get("ID") is not None and not _tag(el).endswith("Values"):
                refs.add(el)
            continue

        if tag == "DateOfIssue":
            meta["dateOfIssue"] = _ymd(el)
        elif tag == "Location":
            locations[el.get("ID") or ""] = _location_info(el, refs)
            el.clear()
        elif tag == "IDRegDocument":
            label = refs.get("IDRegDocType", el.get("IDRegDocTypeID"))
            number = _text(_child(el, "IDRegistrationNo"))
            if number:
                docs_by_identity.setdefault(el.get("IdentityID") or "", []).append(
                    {
                        "type": id_type_of(label),
                        "label": label,
                        "value": normalize_id_value(number),
                        "raw": number,
                        "country": refs.get("Country", el.get("IssuedBy-CountryID")),
                    }
                )
            el.clear()
        elif tag == "DistinctParty":
            party = _parse_party(el, refs, locations)
            if party:
                parties[party["extId"]] = party
                for identity_id in party.pop("_identities"):
                    identity_to_party[identity_id] = party["extId"]
                for profile_id in party.pop("_profiles"):
                    profile_to_party[profile_id] = party["extId"]
            el.clear()
        elif tag == "ProfileRelationship":
            relations.append(
                (
                    el.get("From-ProfileID") or "",
                    el.get("To-ProfileID") or "",
                    refs.get("RelationType", el.get("RelationTypeID")),
                )
            )
            el.clear()
        elif tag == "SanctionsEntry":
            ext_id = profile_to_party.get(el.get("ProfileID") or "")
            party = parties.get(ext_id or "")
            if party is not None:
                list_name = refs.get("List", el.get("ListID"))
                if list_name and list_name not in party["lists"]:
                    party["lists"].append(list_name)
                for ev in _children(el, "EntryEvent"):
                    day = _ymd(_child(ev, "Date"))
                    if day and (not party["designatedOn"] or day < party["designatedOn"]):
                        party["designatedOn"] = day
                for measure in _children(el, "SanctionsMeasure"):
                    stype = refs.get("SanctionsType", measure.get("SanctionsTypeID"))
                    comment = _text(_child(measure, "Comment"))
                    if stype == "Program" and comment and comment not in party["programs"]:
                        party["programs"].append(comment)
            el.clear()

    for identity_id, docs in docs_by_identity.items():
        party = parties.get(identity_to_party.get(identity_id, ""))
        if party is None:
            continue
        for doc in docs:
            if doc not in party["ids"]:
                party["ids"].append(doc)

    for from_profile, to_profile, relation in relations:
        src, dst = profile_to_party.get(from_profile), profile_to_party.get(to_profile)
        if not src or not dst or src not in parties or dst not in parties:
            continue
        parties[src]["linked"].append(
            {"extId": dst, "name": parties[dst]["name"], "relation": relation, "direction": "to"}
        )
        parties[dst]["linked"].append(
            {"extId": src, "name": parties[src]["name"], "relation": relation, "direction": "from"}
        )

    records = [finalize_record(parties[k]) for k in sorted(parties, key=_ext_sort)]
    meta["parties"] = len(records)
    return meta, records


def _ext_sort(ext_id: str) -> tuple[int, str]:
    return (int(ext_id), "") if ext_id.isdigit() else (1 << 62, ext_id)


def _parse_party(el: ET.Element, refs: _Refs, locations: dict[str, dict[str, str]]) -> dict[str, Any] | None:
    ext_id = el.get("FixedRef") or ""
    profile = _child(el, "Profile")
    if not ext_id or profile is None:
        return None
    party_type = _party_type(refs, profile.get("PartySubTypeID"))
    party: dict[str, Any] = {
        "extId": ext_id,
        "partyType": party_type,
        "name": "",
        "aliases": [],
        "lists": [],
        "programs": [],
        "designatedOn": "",
        "ids": [],
        "birthDates": [],
        "nationalities": [],
        "countries": [],
        "addresses": [],
        "linked": [],
        "features": {},
        "_identities": [],
        "_profiles": [profile.get("ID") or ext_id],
    }
    for identity in _children(profile, "Identity"):
        party["_identities"].append(identity.get("ID") or "")
        part_types: dict[str, str] = {}
        groups = _child(identity, "NamePartGroups")
        if groups is not None:
            for master in groups:
                for group in master:
                    part_types[group.get("ID") or ""] = refs.get("NamePartType", group.get("NamePartTypeID"))
        for alias in _children(identity, "Alias"):
            alias_type = refs.get("AliasType", alias.get("AliasTypeID"))
            for name_el in _children(alias, "DocumentedName"):
                name = _documented_name(refs, name_el, part_types, party_type)
                if not name:
                    continue
                if alias.get("Primary") == "true" and identity.get("Primary") == "true" and not party["name"]:
                    party["name"] = name
                    continue
                entry = {
                    "name": name,
                    "type": _ALIAS_TYPES.get(alias_type, "aka"),
                    "lowQuality": alias.get("LowQuality") == "true",
                }
                if entry not in party["aliases"] and name != party["name"]:
                    party["aliases"].append(entry)

    for feature in _children(profile, "Feature"):
        label = refs.get("FeatureType", feature.get("FeatureTypeID")).rstrip(":").strip()
        for version in _children(feature, "FeatureVersion"):
            value = _feature_value(version, refs, locations)
            if not value:
                continue
            if label == "Birthdate":
                _add(party["birthDates"], value)
            elif label in _COUNTRY_FEATURES:
                _add(party["nationalities"], value)
            elif label == "Location":
                loc = _location_of(version, locations)
                if loc.get("country"):
                    _add(party["countries"], loc["country"])
                if loc.get("address"):
                    _add(party["addresses"], ", ".join(x for x in (loc["address"], loc.get("country", "")) if x))
            else:
                _add(party["features"].setdefault(label, []), value)

    if not party["name"]:
        latin = next((a["name"] for a in party["aliases"] if not a["lowQuality"]), "")
        party["name"] = latin or (party["aliases"][0]["name"] if party["aliases"] else "")
    return party if party["name"] else None


def _location_of(version: ET.Element, locations: dict[str, dict[str, str]]) -> dict[str, str]:
    ref = _child(version, "VersionLocation")
    return locations.get(ref.get("LocationID") or "", {}) if ref is not None else {}


def _feature_value(version: ET.Element, refs: _Refs, locations: dict[str, dict[str, str]]) -> str:
    period = _child(version, "DatePeriod")
    if period is not None:
        return _date_period(period)
    loc = _location_of(version, locations)
    if loc:
        return loc.get("country") or loc.get("address") or ""
    values: list[str] = []
    for detail in _children(version, "VersionDetail"):
        text = _text(detail) or refs.get("DetailReference", detail.get("DetailReferenceID"))
        if text:
            values.append(text)
    return "; ".join(values)


def _add(bucket: list[str], value: str) -> None:
    if value and value not in bucket:
        bucket.append(value)


def finalize_record(party: dict[str, Any]) -> dict[str, Any]:
    record = {k: v for k, v in party.items() if not k.startswith("_")}
    record["lists"] = sorted(record["lists"])
    record["programs"] = sorted(record["programs"])
    record["ids"] = sorted(record["ids"], key=lambda d: (d["type"], d["value"], d["country"]))
    record["linked"] = sorted(record["linked"], key=lambda d: (d["extId"], d["direction"], d["relation"]))
    record["contentHash"] = content_hash(record)
    return record


def content_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "contentHash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def merge_records(*record_lists: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """SDN + Consolidated: one record per extId, lists/programs/ids/aliases united."""
    merged: dict[str, dict[str, Any]] = {}
    for records in record_lists:
        for rec in records:
            cur = merged.get(rec["extId"])
            if cur is None:
                merged[rec["extId"]] = json.loads(json.dumps(rec))
                continue
            for key in ("lists", "programs", "birthDates", "nationalities", "countries", "addresses"):
                for v in rec[key]:
                    _add(cur[key], v)
            for key in ("aliases", "ids", "linked"):
                for v in rec[key]:
                    if v not in cur[key]:
                        cur[key].append(v)
            for label, values in rec["features"].items():
                for v in values:
                    _add(cur["features"].setdefault(label, []), v)
            if rec["designatedOn"] and (not cur["designatedOn"] or rec["designatedOn"] < cur["designatedOn"]):
                cur["designatedOn"] = rec["designatedOn"]
    return [finalize_record(merged[k]) for k in sorted(merged, key=_ext_sort)]
