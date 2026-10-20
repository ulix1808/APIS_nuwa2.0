"""
Integration tests of the daily early-warning match (watchlist_match + handler_watchlist_match) on PostgreSQL.
Skipped unless NUWA_TEST_PG is set (see tests/test_watchlist_db.py).
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

import handler_watchlist_match as hm  # noqa: E402
import watchlist_db as db  # noqa: E402
import watchlist_match as wm  # noqa: E402
import watchlist_ofac as ofac  # noqa: E402

CONNINFO = os.environ.get("NUWA_TEST_PG")
pytestmark = pytest.mark.skipif(not CONNINFO, reason="NUWA_TEST_PG not set")
FIXTURES = ROOT / "tests" / "fixtures" / "ofac"
MIGRATIONS = ["20261020000000_watchlist_lists.sql", "20261021000000_watchlist_match.sql"]

BASE_SCHEMA = """
drop schema public cascade; create schema public;
create extension if not exists pgcrypto;
create table public.companies (client_id integer primary key, name text);
create table public.entities (id uuid primary key default gen_random_uuid(), client_id integer not null references public.companies(client_id),
  name text not null, party_type text not null, rfc text, curp text, status text not null default 'active',
  last_report_folio text, deleted_at timestamptz, created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table public.entity_alerts (id uuid primary key default gen_random_uuid(), client_id integer not null, entity_id uuid not null references public.entities(id) on delete cascade,
  alert_type text not null, severity text not null, title text not null, description text, payload jsonb not null default '{}',
  status text not null default 'new', created_at timestamptz not null default now(),
  constraint entity_alerts_alert_type_check check (alert_type in ('risk_change','new_match','new_media_mention','status_change','run_failed')));
create table public.reports (id uuid primary key default gen_random_uuid(), folio text not null, client_id integer not null,
  report_json jsonb not null default '{}', status text not null default 'active');
create table public.entity_relations (id bigserial primary key, client_id integer not null, report_folio text not null,
  status text not null default 'active', related_name text not null, related_party_type text, related_aliases jsonb not null default '[]',
  relation_type text, created_at timestamptz not null default now());
create table public.sources (id bigserial primary key, name text, risk_level smallint);
create table public.nuwa_roles (id smallint primary key, slug text);
create table public.nuwa_users (id bigserial primary key, client_id integer, role_id smallint, email text, is_active boolean);
create table public.risk_entity_chunks (id uuid primary key default gen_random_uuid(), source_id bigint not null, chunk_text text not null,
  row_hash text, first_seen_at timestamptz, created_at timestamptz not null default now());
"""


@pytest.fixture()
def conn():
    c = psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True)
    c.execute(BASE_SCHEMA)
    for name in MIGRATIONS:
        c.execute((ROOT / "supabase" / "migrations" / name).read_text())
    c.execute("insert into companies values (8, 'QA'), (9, 'Otro cliente')")
    c.execute("insert into nuwa_roles values (1, 'super_admin'), (2, 'analyst')")
    c.execute(
        "insert into nuwa_users (client_id, role_id, email, is_active) values "
        "(8, 2, 'QA@Nuwa.space', true), (8, 2, 'old@nuwa.space', false), (9, 2, 'ana@otro.mx', true), (1, 1, 'root@nuwa.space', true)"
    )
    yield c
    c.close()


def _ofac_records():
    _, sdn = ofac.parse_advanced_xml(str(FIXTURES / "SDN_SAMPLE.XML"))
    _, cons = ofac.parse_advanced_xml(str(FIXTURES / "CONS_SAMPLE.XML"))
    return ofac.merge_records(sdn, cons)


def _sync(conn, records):
    run_id = db.start_run(conn, "sync", "ofac", "manual")
    counts = db.apply_sync(conn, "ofac", records, run_id)
    db.finish_run(conn, run_id, "ok", counts=counts)


def _entity(conn, client_id, name, party_type, rfc=None, curp=None, folio=None):
    return str(
        conn.execute(
            "insert into entities (client_id, name, party_type, rfc, curp, last_report_folio) values (%s,%s,%s,%s,%s,%s) returning id",
            (client_id, name, party_type, rfc, curp, folio),
        ).fetchone()["id"]
    )


def _report(conn, client_id, folio, report_json):
    conn.execute("insert into reports (folio, client_id, report_json) values (%s,%s,%s)", (folio, client_id, json.dumps(report_json)))


def _sat(conn):
    sid = conn.execute("insert into sources (name, risk_level) values ('SAT – Contribuyentes Incumplidos – Artículo 69B - Definitivos', 3) returning id").fetchone()["id"]
    conn.execute("insert into sources (name, risk_level) values ('SAT – Medio', 1)")
    conn.execute("insert into sources (name, risk_level) values ('CNBV - Sanciones', 2)")
    for rfc, name in (("FSE970213LF7", "FRACTAL SERVICIOS, S.A. DE C.V."), ("TOSO840429TB6", "TORRIJOS SANCHEZ OSCAR MANUEL")):
        conn.execute(
            "insert into risk_entity_chunks (source_id, chunk_text, row_hash) values (%s, %s, %s)",
            (sid, json.dumps({"id": rfc, "nombre": name, "Situación del contribuyente": "Definitivo"}), f"h-{rfc}"),
        )
    return sid


def _run(conn, monkeypatch, **event):
    monkeypatch.setattr(hm.db, "connect", lambda **_: psycopg.connect(CONNINFO, row_factory=dict_row, autocommit=True))
    sent: list[dict] = []  # one mail per client (SES)
    monkeypatch.setattr(
        hm.mail,
        "mail_matches",
        lambda recipients, alerts: sent.append({"clientId": alerts[0]["clientId"], "recipients": recipients, "alerts": alerts})
        or True,
    )
    bells: list[dict] = []  # the BFF only pushes the bell
    monkeypatch.setattr(hm, "_post_bff", lambda payload: bells.append(payload) or 200)
    out = hm.handler({"trigger": "manual", **event}, None)
    assert out["ok"], out
    assert sorted(p["clientId"] for p in bells) == sorted(p["clientId"] for p in sent)
    assert all(p["event"] == "matches" and p["mailed"] and "recipients" not in p for p in bells)
    return out, sent


def _alerts(conn):
    return conn.execute(
        "select a.*, e.name entity_name from entity_alerts a join entities e on e.id = a.entity_id order by e.name"
    ).fetchall()


def _setup_jalisco(conn):
    _sync(conn, _ofac_records())
    sat_id = _sat(conn)
    green = _entity(conn, 8, "Green Agropacific", "organization")
    # Its last report already shows the OFAC hit (OpenSanctions): not alerted again.
    _report(conn, 8, "SE-AMATEQ", {"reportJson": {"origenResults": [{"name": "Distribuidora Imperial de Baja California S.A. de C.V.", "details": {"isOFAC": True}}]}})
    imperial = _entity(conn, 8, "Distribuidora Imperial de Baja California", "organization", folio="SE-AMATEQ")
    liliana_no_id = _entity(conn, 8, "Liliana Cisneros Tapia", "individual")
    liliana = _entity(conn, 9, "Liliana Cisneros Tapia", "individual", curp="CITL801109MMNSPL07")
    # A company whose report lists Green Agropacific as shareholder, and Liliana (no ID) as legal rep.
    _report(conn, 8, "SE-HOLDING", {"reportJson": {}})
    holding = _entity(conn, 8, "Holding Pacifico Norte", "organization", folio="SE-HOLDING")
    for name, ptype, rtype in (
        ("Green Agropacific SPR de RL de CV", "organization", "shareholder"),
        ("Liliana Cisneros Tapia", "individual", "legal_rep"),
        ("Green Agropacific", "organization", "media_mention"),
    ):
        conn.execute(
            "insert into entity_relations (client_id, report_folio, related_name, related_party_type, relation_type) values (8, 'SE-HOLDING', %s, %s, %s)",
            (name, ptype, rtype),
        )
    fractal = _entity(conn, 8, "Fractal Servicios", "organization", rfc="FSE970213LF7")
    torrijos_homonym = _entity(conn, 8, "Oscar Manuel Torrijos Sanchez", "individual", rfc="TOSO840429XX1")
    return {"green": green, "imperial": imperial, "liliana_no_id": liliana_no_id, "liliana": liliana, "holding": holding,
            "fractal": fractal, "torrijos": torrijos_homonym, "sat_id": sat_id}


def test_first_run_alerts_once_per_entity_and_skips_what_the_report_shows(conn, monkeypatch):
    ids = _setup_jalisco(conn)
    out, sent = _run(conn, monkeypatch)
    stats = out["counts"]
    assert stats["ofacRecords"] == 9 and stats["satRecords"] == 2  # 10 records, the vessel is out
    alerts = {a["entity_name"]: a for a in _alerts(conn)}
    assert set(alerts) == {"Green Agropacific", "Holding Pacifico Norte", "Liliana Cisneros Tapia", "Fractal Servicios"}

    green = alerts["Green Agropacific"]
    assert green["alert_type"] == "list_match" and green["severity"] == "high" and green["client_id"] == 8
    assert green["title"] == "Green Agropacific aparece en OFAC SDN: Green Agropacific S.P.R. de R.L. de C.V."
    m = green["payload"]["matches"][0]
    assert (m["recordRef"], m["matchedBy"], m["designatedOn"]) == ("58020", "name", "2026-07-23")
    assert green["description"] == (
        "Green Agropacific → OFAC SDN: Green Agropacific S.P.R. de R.L. de C.V. (por nombre, designada el 23 jul 2026)."
    )

    holding = alerts["Holding Pacifico Norte"]
    assert holding["title"].startswith("Accionista de Holding Pacifico Norte (Green Agropacific SPR de RL de CV) aparece en OFAC SDN")
    assert [x["subjectRole"] for x in holding["payload"]["matches"]] == ["shareholder"]  # no ID for the legal rep, no media

    liliana = alerts["Liliana Cisneros Tapia"]
    assert liliana["client_id"] == 9 and liliana["payload"]["matches"][0]["matchedBy"] == "curp"

    fractal = alerts["Fractal Servicios"]["payload"]["matches"][0]
    assert (fractal["listCode"], fractal["matchedBy"], fractal["rfc"]) == ("sat", "rfc", "FSE970213LF7")

    in_report = conn.execute("select status from watchlist_matches where entity_id = %s", (ids["imperial"],)).fetchone()
    assert in_report["status"] == "in_report"
    assert stats["alerts"] == 4 and stats["inReport"] == 1
    # One mail per client, to its active users; the BFF gets the same alerts for the bell.
    by_client = {p["clientId"]: p for p in sent}
    assert sorted(by_client) == [8, 9]
    assert by_client[8]["recipients"] == ["qa@nuwa.space"] and by_client[9]["recipients"] == ["ana@otro.mx"]
    mail_alert = next(a for a in by_client[8]["alerts"] if a["entityName"] == "Green Agropacific")
    assert mail_alert["partyType"] == "organization" and mail_alert["matches"][0]["recordName"].startswith("Green Agropacific")
    assert stats["mails"] == {"8": 1, "9": 1} and stats["bells"] == {"8": 200, "9": 200}


def test_second_run_repeats_nothing_and_new_things_are_matched(conn, monkeypatch):
    ids = _setup_jalisco(conn)
    _run(conn, monkeypatch)
    out, sent = _run(conn, monkeypatch)
    assert out["counts"]["alerts"] == 0 and sent == []
    assert out["counts"]["ofacNew"] == 0 and out["counts"]["satNew"] == 0

    # B: an entity registered after the last run is checked against the whole list.
    _entity(conn, 8, "Agropecuaria Green Agropacific", "organization")
    _entity(conn, 8, "Miguel Aguilar Amao", "individual", rfc="AUAM530929KQ2")
    out, _ = _run(conn, monkeypatch)
    assert [a["entityName"] for a in out["alerts"]] == ["Miguel Aguilar Amao"]

    # A: a record added to OFAC after the last run is checked against every entity.
    records = _ofac_records()
    newcomer = copy.deepcopy(next(r for r in records if r["extId"] == "58020"))
    newcomer.update(extId="99999", name="Holding Pacifico Norte S.A. de C.V.", aliases=[], ids=[], linked=[])
    _sync(conn, records + [ofac.finalize_record(newcomer)])
    out, _ = _run(conn, monkeypatch)
    assert [(a["entityName"], a["matches"][0]["recordRef"]) for a in out["alerts"]] == [("Holding Pacifico Norte", "99999")]
    assert out["counts"]["ofacNew"] == 1


def test_a_dismissed_alert_comes_back_only_if_the_record_changes(conn, monkeypatch):
    ids = _setup_jalisco(conn)
    _run(conn, monkeypatch)
    conn.execute("update entity_alerts set status = 'dismissed' where entity_id = %s", (ids["green"],))
    out, _ = _run(conn, monkeypatch, full=True)
    assert out["counts"]["alerts"] == 0 and out["counts"]["dismissed"] >= 1

    records = _ofac_records()
    green = next(r for r in records if r["extId"] == "58020")
    green["programs"].append("SDNTK")
    records[records.index(green)] = ofac.finalize_record(green)
    _sync(conn, records)
    out, _ = _run(conn, monkeypatch)
    # The dismissed one comes back; the partner alert of the holding is still open, so it is not repeated.
    assert [a["entityName"] for a in out["alerts"]] == ["Green Agropacific"]


def test_client_filter_of_the_dev_stack(conn, monkeypatch):
    _setup_jalisco(conn)
    monkeypatch.setenv("WATCHLIST_CLIENT_IDS", "9")
    out, sent = _run(conn, monkeypatch)
    assert [a["clientId"] for a in out["alerts"]] == [9] and [p["clientId"] for p in sent] == [9]
    assert out["counts"]["clientFilter"] == [9]


def test_a_client_filtered_run_does_not_move_the_cursor_of_the_others(conn, monkeypatch):
    _setup_jalisco(conn)
    monkeypatch.setenv("WATCHLIST_CLIENT_IDS", "9")
    _run(conn, monkeypatch)
    assert wm.last_match_since(conn) is None
    # The first run over every client still matches client 8 against the whole list.
    monkeypatch.delenv("WATCHLIST_CLIENT_IDS")
    out, _ = _run(conn, monkeypatch)
    assert 8 in {a["clientId"] for a in out["alerts"]}
    assert wm.last_match_since(conn) is not None


def test_inactive_and_deleted_entities_are_not_matched(conn, monkeypatch):
    _sync(conn, _ofac_records())
    gone = _entity(conn, 8, "Green Agropacific", "organization")
    conn.execute("update entities set deleted_at = now() where id = %s", (gone,))
    _entity(conn, 8, "Green Agropacific", "organization")
    conn.execute("update entities set status = 'inactive' where deleted_at is null")
    out, _ = _run(conn, monkeypatch)
    assert out["counts"]["alerts"] == 0 and out["counts"]["entities"] == 0


def test_report_known_reads_opensanctions_and_sat_hits():
    known = wm.report_known(
        {
            "reportJson": {
                "origenResults": [
                    {
                        "name": "Green Agropacific S.P.R. de R.L. de C.V.",
                        "details": {"isOFAC": True, "relatedEntities": [{"name": "Liliana Cisneros Tapia"}]},
                    },
                    {"name": "Otra Empresa", "category": "PEP"},
                ],
                "listResults": [{"sourceId": "62", "identifier": "fse970213lf7", "chunkId": "c-1"}],
            }
        }
    )
    assert "greenagropacific" in known.ofac_name_keys
    assert "cisneros liliana tapia" in known.ofac_name_keys
    assert "otraempresa" not in known.ofac_name_keys
    assert known.sat_refs == {"62:FSE970213LF7"} and known.chunk_ids == {"c-1"}


def test_status_for_the_admin_panel(conn, monkeypatch):
    _setup_jalisco(conn)
    _run(conn, monkeypatch)
    st = db.status(conn)
    assert st["entries"]["active"] == 10 and st["entries"]["organizations"] >= 3
    assert st["lastSync"]["status"] == "ok" and st["lastMatch"]["status"] == "ok"
    assert len(st["lastChanges"]) == 9  # the first sync added everything but the vessel is not listed
    assert {c["clientId"] for c in st["alertsByClient"]} == {8, 9}
    assert st["recentAlerts"][0]["title"]
    assert db.super_admin_emails(conn) == ["root@nuwa.space"]
