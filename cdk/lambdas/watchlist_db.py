"""
Database side of the early-warning lists (public.watchlist_*).

apply_sync() writes one refresh of a list in a single transaction, so readers never see half a list:
new records are inserted ('added'), records whose content changed are updated ('changed', previous version
kept in watchlist_entry_changes), records missing from the file get removed_at ('removed'), and a removed
record that comes back is 'restored'. Unchanged records only move last_seen_at.
A file with far fewer records than the active list is refused (broken download or format change): the
last good version stays.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from nuwa_config import get_database_config
from watchlist_names import normalize_text

MIN_KEEP_RATIO = 0.5

ENTRY_COLUMNS = (
    "list_code, ext_id, party_type, name, name_norm, aliases, lists, programs, designated_on, "
    "birth_dates, nationalities, countries, details, content_hash"
)


class SyncRefused(Exception):
    """The new file would remove too much of the list; nothing was written."""


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    cfg = get_database_config()
    return psycopg.connect(
        f"host={cfg['host']} port={cfg['port']} dbname={cfg['dbname']} user={cfg['user']} "
        f"password={cfg['password']} sslmode={cfg['sslmode']}",
        row_factory=dict_row,
        autocommit=autocommit,
    )


def _row_values(list_code: str, rec: dict[str, Any]) -> tuple:
    designated = rec.get("designatedOn") or ""
    designated_on = dt.date.fromisoformat(designated) if len(designated) == 10 else None
    details = {
        "ids": rec.get("ids", []),
        "addresses": rec.get("addresses", []),
        "linked": rec.get("linked", []),
        "features": rec.get("features", {}),
    }
    return (
        list_code,
        rec["extId"],
        rec["partyType"],
        rec["name"],
        normalize_text(rec["name"]),
        Json(rec.get("aliases", [])),
        rec.get("lists", []),
        rec.get("programs", []),
        designated_on,
        rec.get("birthDates", []),
        rec.get("nationalities", []),
        rec.get("countries", []),
        Json(details),
        rec["contentHash"],
    )


def start_run(conn: psycopg.Connection, kind: str, list_code: str | None, trigger: str) -> int:
    row = conn.execute(
        "INSERT INTO public.watchlist_runs (kind, list_code, trigger) VALUES (%s, %s, %s) RETURNING id",
        (kind, list_code, trigger),
    ).fetchone()
    return int(row["id"])


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    *,
    counts: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    published_on: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE public.watchlist_runs
           SET status = %s, counts = %s, files = %s, published_on = %s, error = %s, finished_at = now()
         WHERE id = %s
        """,
        (
            status,
            Json(counts or {}),
            Json(files or {}),
            dt.date.fromisoformat(published_on) if published_on else None,
            (error or None) and error[:4000],
            run_id,
        ),
    )


def last_good_sync(conn: psycopg.Connection, list_code: str) -> dict[str, Any] | None:
    """Last sync that read the files (ok or unchanged), for the 'did the publication change?' check."""
    return conn.execute(
        """
        SELECT id, status, files, published_on, finished_at FROM public.watchlist_runs
         WHERE kind = 'sync' AND list_code = %s AND status IN ('ok', 'unchanged') AND files <> '{}'::jsonb
         ORDER BY id DESC LIMIT 1
        """,
        (list_code,),
    ).fetchone()


def _replace_ids(cur: psycopg.Cursor, entry_ids: dict[str, int], records: list[dict[str, Any]]) -> None:
    if not records:
        return
    ids = [entry_ids[r["extId"]] for r in records]
    cur.execute("DELETE FROM public.watchlist_entry_ids WHERE entry_id = ANY(%s)", (ids,))
    rows = []
    for rec in records:
        seen: set[tuple[str, str]] = set()
        for doc in rec.get("ids", []):
            key = (doc["type"], doc["value"])
            if not doc["value"] or key in seen:
                continue
            seen.add(key)
            rows.append((entry_ids[rec["extId"]], doc["type"], doc["value"], doc.get("label"), doc.get("country")))
    if rows:
        cur.executemany(
            "INSERT INTO public.watchlist_entry_ids (entry_id, id_type, id_value, label, country) VALUES (%s, %s, %s, %s, %s)",
            rows,
        )


def apply_sync(
    conn: psycopg.Connection,
    list_code: str,
    records: list[dict[str, Any]],
    run_id: int,
    *,
    min_keep_ratio: float = MIN_KEEP_RATIO,
) -> dict[str, int]:
    """One transaction. Returns {total, added, changed, removed, restored, unchanged}."""
    by_ext = {r["extId"]: r for r in records}
    counts = {"total": len(by_ext), "added": 0, "changed": 0, "removed": 0, "restored": 0, "unchanged": 0}
    with conn.transaction():
        cur = conn.cursor()
        existing = {
            row["ext_id"]: row
            for row in cur.execute(
                "SELECT id, ext_id, content_hash, removed_at FROM public.watchlist_entries WHERE list_code = %s FOR UPDATE",
                (list_code,),
            ).fetchall()
        }
        active = sum(1 for r in existing.values() if r["removed_at"] is None)
        if active and len(by_ext) < active * min_keep_ratio:
            raise SyncRefused(
                f"La lista nueva trae {len(by_ext)} registros y hay {active} activos: no se aplica (mínimo {min_keep_ratio:.0%})."
            )

        added = [r for k, r in by_ext.items() if k not in existing]
        restored = [r for k, r in by_ext.items() if k in existing and existing[k]["removed_at"] is not None]
        changed = [
            r
            for k, r in by_ext.items()
            if k in existing and existing[k]["removed_at"] is None and existing[k]["content_hash"] != r["contentHash"]
        ]
        unchanged = [
            k
            for k, r in by_ext.items()
            if k in existing and existing[k]["removed_at"] is None and existing[k]["content_hash"] == r["contentHash"]
        ]
        removed = [row for k, row in existing.items() if k not in by_ext and row["removed_at"] is None]

        entry_ids = {k: int(row["id"]) for k, row in existing.items()}
        changes: list[tuple[int, int, str, Any]] = []

        if added:
            placeholders = ", ".join(["%s"] * 14)
            cur.executemany(
                f"INSERT INTO public.watchlist_entries ({ENTRY_COLUMNS}) VALUES ({placeholders})",
                [_row_values(list_code, r) for r in added],
            )
            for row in cur.execute(
                "SELECT id, ext_id FROM public.watchlist_entries WHERE list_code = %s AND ext_id = ANY(%s)",
                (list_code, [r["extId"] for r in added]),
            ).fetchall():
                entry_ids[row["ext_id"]] = int(row["id"])
            changes += [(entry_ids[r["extId"]], run_id, "added", None) for r in added]

        updated = changed + restored
        if updated:
            previous = {
                row["id"]: row["prev"]
                for row in cur.execute(
                    "SELECT id, to_jsonb(e) - 'id' AS prev FROM public.watchlist_entries e WHERE id = ANY(%s)",
                    ([entry_ids[r["extId"]] for r in updated],),
                ).fetchall()
            }
            cur.executemany(
                """
                UPDATE public.watchlist_entries
                   SET party_type = %s, name = %s, name_norm = %s, aliases = %s, lists = %s, programs = %s,
                       designated_on = %s, birth_dates = %s, nationalities = %s, countries = %s, details = %s,
                       content_hash = %s, last_seen_at = now(), changed_at = now(), removed_at = NULL
                 WHERE id = %s
                """,
                [(*_row_values(list_code, r)[2:], entry_ids[r["extId"]]) for r in updated],
            )
            changes += [(entry_ids[r["extId"]], run_id, "changed", Json(previous.get(entry_ids[r["extId"]]))) for r in changed]
            changes += [(entry_ids[r["extId"]], run_id, "restored", Json(previous.get(entry_ids[r["extId"]]))) for r in restored]

        _replace_ids(cur, entry_ids, added + updated)

        if unchanged:
            cur.execute(
                "UPDATE public.watchlist_entries SET last_seen_at = now() WHERE list_code = %s AND ext_id = ANY(%s)",
                (list_code, unchanged),
            )

        if removed:
            removed_ids = [int(row["id"]) for row in removed]
            previous = {
                row["id"]: row["prev"]
                for row in cur.execute(
                    "SELECT id, to_jsonb(e) - 'id' AS prev FROM public.watchlist_entries e WHERE id = ANY(%s)",
                    (removed_ids,),
                ).fetchall()
            }
            cur.execute(
                "UPDATE public.watchlist_entries SET removed_at = now(), changed_at = now() WHERE id = ANY(%s)",
                (removed_ids,),
            )
            changes += [(i, run_id, "removed", Json(previous.get(i))) for i in removed_ids]

        if changes:
            cur.executemany(
                "INSERT INTO public.watchlist_entry_changes (entry_id, run_id, change, previous) VALUES (%s, %s, %s, %s)",
                changes,
            )

        counts.update(
            added=len(added), changed=len(changed), removed=len(removed), restored=len(restored), unchanged=len(unchanged)
        )
    return counts


def list_runs(conn: psycopg.Connection, *, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, kind, list_code, status, trigger, published_on, files, counts, error, started_at, finished_at
          FROM public.watchlist_runs
         WHERE (%(kind)s::text IS NULL OR kind = %(kind)s)
         ORDER BY id DESC LIMIT %(limit)s
        """,
        {"kind": kind, "limit": max(1, min(int(limit), 200))},
    ).fetchall()
    return [_run_to_api(r) for r in rows]


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def _run_to_api(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "runId": r["id"],
        "kind": r["kind"],
        "listCode": r["list_code"],
        "status": r["status"],
        "trigger": r["trigger"],
        "publishedOn": _iso(r["published_on"]),
        "files": r["files"],
        "counts": r["counts"],
        "error": r["error"],
        "startedAt": _iso(r["started_at"]),
        "finishedAt": _iso(r["finished_at"]),
    }
