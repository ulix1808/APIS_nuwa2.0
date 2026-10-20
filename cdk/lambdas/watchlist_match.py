"""
Daily early-warning match (watchlist-match Lambda, 06:00 CDMX).

Subjects: active entities of every client (or WATCHLIST_CLIENT_IDS) and, from each entity's last report, its
partners and representatives (entity_relations: shareholder, legal_rep, ubo, officer, business_partner).
Lists: our OFAC copy (watchlist_entries) and the SAT sources of risk Alto/Crítico (risk_entity_chunks).

Two crosses, so nothing new on either side goes unchecked:
  A. list records added / changed / restored since the last match run → every subject;
  B. subjects new or edited since the last match run → the whole lists.
The first run (no previous match run) is A over the whole lists.

Rules (watchlist_index.evaluate): IDs decide, people need an ID, companies match by name without legal forms.
Not alerted: what the entity's last report already shows (the screening saw it), a match already alerted for
the same list record, and a dismissed alert while the list record did not change.
One alert per entity per run (entity_alerts, alert_type list_match) with all its new matches.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Json

import watchlist_index as wi
from watchlist_names import IdSet, company_tokens, normalize_id, person_tokens

PARTNER_RELATION_TYPES = ("shareholder", "legal_rep", "ubo", "officer", "business_partner")
SAT_MIN_RISK_LEVEL = 2  # 2 = alto, 3 = crítico
INACTIVE_ENTITY_STATUSES = ("deleted", "inactive")

ROLE_LABELS = {
    "shareholder": "Accionista",
    "legal_rep": "Representante legal",
    "ubo": "Beneficiario final",
    "officer": "Directivo",
    "business_partner": "Socio comercial",
}
MATCHED_BY_LABELS = {
    "rfc": "RFC",
    "curp": "CURP",
    "rfc_curp": "RFC y CURP coinciden en fecha e iniciales",
    "name": "nombre",
    "name_partial": "nombre (la lista agrega palabras al final)",
    "name_fuzzy": "nombre con una letra de diferencia",
}


# ---------------------------------------------------------------------------------------------- lists


def load_ofac(conn: psycopg.Connection, since: dt.datetime | None) -> tuple[list[wi.ListRecord], set[str]]:
    """All active OFAC people/companies, and the refs added/changed/restored since `since`."""
    rows = conn.execute(
        """
        SELECT ext_id, party_type, name, aliases, lists, programs, designated_on, birth_dates, nationalities,
               countries, details, content_hash
          FROM public.watchlist_entries
         WHERE list_code = 'ofac' AND removed_at IS NULL AND party_type IN ('individual', 'organization')
        """
    ).fetchall()
    records = [wi.ofac_record_from_row(r) for r in rows]
    if since is None:
        return records, {r.ref for r in records}
    changed = {
        r["ext_id"]
        for r in conn.execute(
            """
            SELECT DISTINCT e.ext_id FROM public.watchlist_entry_changes c
              JOIN public.watchlist_entries e ON e.id = c.entry_id
             WHERE e.list_code = 'ofac' AND c.change IN ('added', 'changed', 'restored') AND c.at > %s
            """,
            (since,),
        ).fetchall()
    }
    return records, changed


_SAT_NAME_KEYS = ("nombre", "nombre del contribuyente", "razon social", "razón social", "name")
_SAT_RFC_KEYS = ("id", "rfc", "r.f.c.")


def sat_record_from_chunk(chunk: dict[str, Any], source_name: str) -> wi.ListRecord | None:
    try:
        obj = json.loads(chunk["chunk_text"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    lower = {str(k).strip().lower(): v for k, v in obj.items() if k != "chunk_text_normalized"}
    name = next((str(lower[k]).strip() for k in _SAT_NAME_KEYS if lower.get(k)), "")
    rfc = next((normalize_id(str(lower[k])) for k in _SAT_RFC_KEYS if lower.get(k)), "")
    if not name and not rfc:
        return None
    party_type = {13: "individual", 12: "organization"}.get(len(rfc), "unknown")
    ref = f"{chunk['source_id']}:{rfc or chunk['id']}"
    content_hash = chunk.get("row_hash") or hashlib.sha256(chunk["chunk_text"].encode("utf-8")).hexdigest()
    fields = {k: v for k, v in obj.items() if k not in ("chunk_text_normalized",) and isinstance(v, (str, int, float))}
    return wi.ListRecord(
        ref=ref,
        list_code="sat",
        party_type=party_type,
        name=name or rfc,
        names=[name] if name else [],
        ids=IdSet.build(rfc=[rfc] if rfc else []),
        content_hash=content_hash,
        info={
            "sourceId": str(chunk["source_id"]),
            "sourceName": source_name,
            "chunkId": str(chunk["id"]),
            "rfc": rfc or None,
            "fields": fields,
        },
    )


def load_sat(conn: psycopg.Connection, since: dt.datetime | None) -> tuple[list[wi.ListRecord], set[str]]:
    sources = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM public.sources WHERE risk_level >= %s AND name ILIKE 'SAT%%'", (SAT_MIN_RISK_LEVEL,)
        ).fetchall()
    }
    if not sources:
        return [], set()
    records: list[wi.ListRecord] = []
    new_refs: set[str] = set()
    # Server-side cursor (~75k chunks): needs a transaction even on an autocommit connection.
    with conn.transaction(), conn.cursor(name="watchlist_sat_chunks") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT id, source_id, chunk_text, row_hash, coalesce(first_seen_at, created_at) AS seen_at
              FROM public.risk_entity_chunks
             WHERE source_id = ANY(%s)
            """,
            (list(sources),),
        )
        for chunk in cur:
            rec = sat_record_from_chunk(chunk, sources[chunk["source_id"]])
            if rec is None:
                continue
            records.append(rec)
            if since is None or chunk["seen_at"] > since:
                new_refs.add(rec.ref)
    return records, new_refs


# ------------------------------------------------------------------------------------------- subjects


@dataclass
class EntitySubjects:
    entity: dict[str, Any]
    subjects: list[tuple[wi.Subject, str | None, bool]] = field(default_factory=list)  # (subject, role, is_new)
    report_json: dict[str, Any] | None = None


def _party(value: str | None) -> str:
    return value if value in ("individual", "organization") else "unknown"


def load_subjects(
    conn: psycopg.Connection, since: dt.datetime | None, client_ids: list[int] | None
) -> list[EntitySubjects]:
    entities = conn.execute(
        """
        SELECT e.id, e.client_id, e.name, e.party_type, e.rfc, e.curp, e.other_ids, e.last_report_folio,
               e.created_at, e.updated_at
          FROM public.entities e
          JOIN public.companies c ON c.client_id = e.client_id
         WHERE e.deleted_at IS NULL AND e.status <> ALL(%(inactive)s)
           AND (%(clients)s::int[] IS NULL OR e.client_id = ANY(%(clients)s))
         ORDER BY e.client_id, e.created_at
        """,
        {"inactive": list(INACTIVE_ENTITY_STATUSES), "clients": client_ids},
    ).fetchall()
    # Last reports and their partners in two queries (not two per entity).
    folios = [(e["client_id"], e["last_report_folio"]) for e in entities if e["last_report_folio"]]
    reports: dict[tuple[int, str], dict[str, Any]] = {}
    relations: dict[tuple[int, str], list[dict[str, Any]]] = {}
    if folios:
        clients_f = [c for c, _ in folios]
        folio_f = [f for _, f in folios]
        for r in conn.execute(
            """
            SELECT r.client_id, r.folio, r.report_json FROM public.reports r
              JOIN unnest(%s::int[], %s::text[]) AS k(client_id, folio) ON k.client_id = r.client_id AND k.folio = r.folio
             WHERE r.status = 'active'
            """,
            (clients_f, folio_f),
        ).fetchall():
            reports[(r["client_id"], r["folio"])] = r["report_json"]
        for rel in conn.execute(
            """
            SELECT x.client_id, x.report_folio, x.related_name, x.related_party_type, x.related_aliases, x.relation_type, x.created_at
              FROM public.entity_relations x
              JOIN unnest(%s::int[], %s::text[]) AS k(client_id, folio) ON k.client_id = x.client_id AND k.folio = x.report_folio
             WHERE x.status = 'active' AND x.relation_type = ANY(%s)
             ORDER BY x.id
            """,
            (clients_f, folio_f, list(PARTNER_RELATION_TYPES)),
        ).fetchall():
            relations.setdefault((rel["client_id"], rel["report_folio"]), []).append(rel)

    out: list[EntitySubjects] = []
    for e in entities:
        changed_at = max(e["created_at"], e["updated_at"] or e["created_at"])
        is_new = since is None or changed_at > since
        item = EntitySubjects(entity=e)
        item.subjects.append(
            (
                wi.Subject("entity", e["name"], _party(e["party_type"]), wi.subject_ids(e["rfc"], e["curp"], e["other_ids"])),
                None,
                is_new,
            )
        )
        key_folio = (e["client_id"], e["last_report_folio"])
        item.report_json = reports.get(key_folio)
        seen_keys: set[str] = set()
        for rel in relations.get(key_folio, []):
            party = _party(rel["related_party_type"])
            key = "rel:" + wi.identity_key(rel["related_name"], party if party != "unknown" else "organization")
            if key == "rel:" or key in seen_keys:
                continue
            seen_keys.add(key)
            aliases = [str(a) for a in (rel["related_aliases"] or []) if isinstance(a, str)]
            rel_new = is_new or rel["created_at"] > since
            item.subjects.append((wi.Subject(key, rel["related_name"], party, IdSet(), aliases), rel["relation_type"], rel_new))
        out.append(item)
    return out


# ------------------------------------------------------------------------------- "already in report"


@dataclass
class ReportKnown:
    ofac_name_keys: set[str] = field(default_factory=set)
    sat_refs: set[str] = field(default_factory=set)
    chunk_ids: set[str] = field(default_factory=set)


_NAME_FIELDS = ("name", "matchedName", "matchedEntity", "entityCaption", "caption")


def _name_keys(name: str) -> set[str]:
    keys = {"".join(company_tokens(name)), " ".join(sorted(person_tokens(name)))}
    return {k for k in keys if k}


def _mentions_ofac(obj: dict[str, Any]) -> bool:
    if obj.get("isOFAC") is True:
        return True
    for k in ("category", "riskCategory", "datasetsRaw", "matchedList", "datasetsFormatted", "source", "sourceName"):
        v = obj.get(k)
        if isinstance(v, str) and ("ofac" in v.lower() or "sdn" in v.lower()):
            return True
    details = obj.get("details")
    return isinstance(details, dict) and details.get("isOFAC") is True


def report_known(report_json: dict[str, Any] | None) -> ReportKnown:
    known = ReportKnown()
    if not report_json:
        return known

    def collect_names(obj: Any) -> None:
        if isinstance(obj, dict):
            for k in _NAME_FIELDS:
                if isinstance(obj.get(k), str) and obj[k].strip():
                    known.ofac_name_keys |= _name_keys(obj[k])
            for v in obj.values():
                collect_names(v)
        elif isinstance(obj, list):
            for v in obj:
                collect_names(v)

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("sourceId") is not None:
                source_id = str(obj["sourceId"])
                ident = normalize_id(str(obj.get("identifier") or ""))
                if ident:
                    known.sat_refs.add(f"{source_id}:{ident}")
                if obj.get("chunkId"):
                    known.chunk_ids.add(str(obj["chunkId"]))
            if _mentions_ofac(obj):
                collect_names(obj)  # the hit and its related people (OFAC "Linked To")
                return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(report_json)
    return known


def in_report(match: wi.Match, known: ReportKnown) -> bool:
    rec = match.record
    if rec.list_code == "sat":
        return rec.ref in known.sat_refs or rec.info.get("chunkId") in known.chunk_ids
    return any(k in known.ofac_name_keys for name in rec.names for k in _name_keys(name))


# ------------------------------------------------------------------------------------------------ run


@dataclass
class NewMatch:
    entity: dict[str, Any]
    subject: wi.Subject
    role: str | None
    match: wi.Match


def find_matches(
    groups: list[EntitySubjects],
    lists: list[tuple[wi.ListIndex, wi.ListIndex]],
) -> list[NewMatch]:
    """lists: (full index, delta index) per list. New subjects go against the full index."""
    found: list[NewMatch] = []
    for group in groups:
        for subject, role, is_new in group.subjects:
            seen: set[tuple[str, str]] = set()
            for full, delta in lists:
                index = full if is_new else delta
                if not len(index):
                    continue
                for m in index.match(subject):
                    key = (m.record.list_code, m.record.ref)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(NewMatch(group.entity, subject, role, m))
    return found


def match_details(nm: NewMatch) -> dict[str, Any]:
    m = nm.match
    return {
        "listCode": m.record.list_code,
        "recordRef": m.record.ref,
        "recordName": m.record.name,
        "recordPartyType": m.record.party_type,
        "subjectKey": nm.subject.key,
        "subjectName": nm.subject.name,
        "subjectRole": nm.role,
        "subjectRoleLabel": ROLE_LABELS.get(nm.role or "", None),
        "matchedBy": m.matched_by,
        "matchedByLabel": MATCHED_BY_LABELS.get(m.matched_by, m.matched_by.upper()),
        "matchedName": m.matched_name,
        "severity": m.severity,
        **m.record.info,
    }


def list_label(d: dict[str, Any]) -> str:
    if d["listCode"] == "sat":
        return d.get("sourceName") or "SAT"
    lists = d.get("lists") or []
    if "SDN List" in lists:
        return "OFAC SDN"
    return "OFAC " + (lists[0] if lists else "Consolidated")


def alert_title(entity_name: str, items: list[dict[str, Any]]) -> str:
    first = items[0]
    who = (
        f"{first['subjectRoleLabel']} de {entity_name} ({first['subjectName']})"
        if first.get("subjectRole")
        else entity_name
    )
    if len(items) == 1:
        return f"{who} aparece en {list_label(first)}: {first['recordName']}"[:300]
    lists = sorted({list_label(i) for i in items})
    return f"{len(items)} coincidencias nuevas de {entity_name} en {', '.join(lists)}"[:300]


_MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def format_date_es(ymd: str | None) -> str:
    """'2026-07-23' → '23 jul 2026' (same as formatListDate in the BFF)."""
    try:
        y, m, d = (int(x) for x in str(ymd or "")[:10].split("-"))
        return f"{d} {_MONTHS_ES[m - 1]} {y}"
    except (ValueError, IndexError):
        return ""


def alert_description(items: list[dict[str, Any]]) -> str:
    lines = []
    for i in items:
        subject = f"{i['subjectRoleLabel']} {i['subjectName']}" if i.get("subjectRole") else i["subjectName"]
        when = f", designada el {format_date_es(i['designatedOn'])}" if i.get("designatedOn") else ""
        review = " Se propone revisar." if i["severity"] == "medium" else ""
        lines.append(f"{subject} → {list_label(i)}: {i['recordName']} (por {i['matchedByLabel']}{when}).{review}")
    return "\n".join(lines)[:4000]


def persist(conn: psycopg.Connection, run_id: int, matches: list[NewMatch], known_by_entity: dict[str, ReportKnown]) -> dict[str, Any]:
    """Dedupe against watchlist_matches, create one alert per entity. Returns stats and the alerts created."""
    stats = {"matches": len(matches), "alerted": 0, "inReport": 0, "repeated": 0, "dismissed": 0, "alerts": 0}
    by_entity: dict[str, list[tuple[NewMatch, dict[str, Any]]]] = {}
    with conn.transaction():
        for nm in matches:
            entity_id = str(nm.entity["id"])
            rec = nm.match.record
            existing = conn.execute(
                """
                SELECT m.id, m.status, m.record_hash, a.status AS alert_status
                  FROM public.watchlist_matches m LEFT JOIN public.entity_alerts a ON a.id = m.alert_id
                 WHERE m.entity_id = %s AND m.subject_key = %s AND m.list_code = %s AND m.record_ref = %s
                """,
                (entity_id, nm.subject.key, rec.list_code, rec.ref),
            ).fetchone()
            details = match_details(nm)
            if existing is not None:
                dismissed = existing["status"] == "dismissed" or existing["alert_status"] == "dismissed"
                same_record = existing["record_hash"] == rec.content_hash
                if same_record or (not dismissed and existing["status"] == "alerted"):
                    conn.execute(
                        "UPDATE public.watchlist_matches SET last_matched_at = now(), status = %s WHERE id = %s",
                        ("dismissed" if dismissed else existing["status"], existing["id"]),
                    )
                    stats["dismissed" if dismissed else "repeated"] += 1
                    continue
            status = "in_report" if in_report(nm.match, known_by_entity.get(entity_id, ReportKnown())) else "alerted"
            row = conn.execute(
                """
                INSERT INTO public.watchlist_matches
                  (client_id, entity_id, subject_key, subject_name, subject_role, list_code, record_ref, record_hash,
                   record_name, matched_by, severity, status, run_id, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, subject_key, list_code, record_ref) DO UPDATE
                   SET record_hash = EXCLUDED.record_hash, record_name = EXCLUDED.record_name,
                       matched_by = EXCLUDED.matched_by, severity = EXCLUDED.severity, status = EXCLUDED.status,
                       run_id = EXCLUDED.run_id, details = EXCLUDED.details, alert_id = NULL, last_matched_at = now()
                RETURNING id
                """,
                (
                    nm.entity["client_id"], entity_id, nm.subject.key, nm.subject.name, nm.role, rec.list_code, rec.ref,
                    rec.content_hash, rec.name, nm.match.matched_by, nm.match.severity, status, run_id, Json(details),
                ),
            ).fetchone()
            if status == "in_report":
                stats["inReport"] += 1
                continue
            stats["alerted"] += 1
            details["matchId"] = row["id"]
            by_entity.setdefault(entity_id, []).append((nm, details))

        alerts: list[dict[str, Any]] = []
        for entity_id, items in by_entity.items():
            entity = items[0][0].entity
            details = [d for _, d in sorted(items, key=lambda x: (x[1]["severity"] != "high", x[1]["listCode"], x[1]["recordName"]))]
            severity = "high" if any(d["severity"] == "high" for d in details) else "medium"
            title = alert_title(entity["name"], details)
            alert = conn.execute(
                """
                INSERT INTO public.entity_alerts (client_id, entity_id, alert_type, severity, title, description, payload)
                VALUES (%s, %s, 'list_match', %s, %s, %s, %s) RETURNING id, created_at
                """,
                (
                    entity["client_id"], entity_id, severity, title, alert_description(details),
                    Json({"source": "watchlist", "runId": run_id, "matches": details}),
                ),
            ).fetchone()
            conn.execute(
                "UPDATE public.watchlist_matches SET alert_id = %s WHERE id = ANY(%s)",
                (alert["id"], [d["matchId"] for d in details]),
            )
            alerts.append(
                {
                    "alertId": str(alert["id"]),
                    "clientId": entity["client_id"],
                    "entityId": entity_id,
                    "entityName": entity["name"],
                    "partyType": entity["party_type"],
                    "severity": severity,
                    "title": title,
                    "matches": details,
                }
            )
        stats["alerts"] = len(alerts)
    return {"stats": stats, "alerts": alerts}


def last_match_since(conn: psycopg.Connection) -> dt.datetime | None:
    """Start of the last successful match run over every client: the next one looks at what changed after it started.

    Runs limited to some clients (the dev stack, a manual {"clientIds": [...]}) do not count: the other clients were
    not matched then. With no such run, None: the first unfiltered run matches everything against the full lists.
    """
    row = conn.execute(
        """
        SELECT started_at FROM public.watchlist_runs
         WHERE kind = 'match' AND status = 'ok'
           AND coalesce(jsonb_typeof(counts -> 'clientFilter'), 'null') = 'null'
         ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    return row["started_at"] if row else None


def run_match(
    conn: psycopg.Connection, run_id: int, *, client_ids: list[int] | None = None, since: dt.datetime | None = None,
    full: bool = False,
) -> dict[str, Any]:
    since = None if full else since
    ofac_records, ofac_new = load_ofac(conn, since)
    sat_records, sat_new = load_sat(conn, since)
    lists = []
    for records, new_refs in ((ofac_records, ofac_new), (sat_records, sat_new)):
        full_index = wi.ListIndex(records)
        delta_index = full_index if since is None else wi.ListIndex(r for r in records if r.ref in new_refs)
        lists.append((full_index, delta_index))
    groups = load_subjects(conn, since, client_ids)
    matches = find_matches(groups, lists)
    known = {str(g.entity["id"]): report_known(g.report_json) for g in groups}
    out = persist(conn, run_id, matches, known)
    out["stats"].update(
        {
            "since": since.isoformat() if since else None,
            "ofacRecords": len(ofac_records),
            "ofacNew": len(ofac_new) if since else None,
            "satRecords": len(sat_records),
            "satNew": len(sat_new) if since else None,
            "entities": len(groups),
            "subjects": sum(len(g.subjects) for g in groups),
            "clients": len({g.entity["client_id"] for g in groups}),
        }
    )
    return out


def group_by_client(alerts: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for a in alerts:
        out.setdefault(int(a["clientId"]), []).append(a)
    return out
