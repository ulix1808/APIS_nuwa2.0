#!/usr/bin/env python3
"""Rellena chunk_text_normalized y actualiza chunk JSON en filas existentes."""

from __future__ import annotations

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas")
sys.path.insert(0, ROOT)

import psycopg
from psycopg.rows import dict_row

from chunk_normalize import prepare_chunk_text_for_storage


def main() -> int:
    conninfo = (
        f"host={os.environ['PGHOST']} "
        f"port={os.environ.get('PGPORT', '5432')} "
        f"dbname={os.environ['PGDATABASE']} "
        f"user={os.environ['PGUSER']} "
        f"password={os.environ['PGPASSWORD']} "
        f"sslmode={os.environ.get('PGSSLMODE', 'require')}"
    )
    batch_size = int(os.environ.get("BATCH_SIZE", "500"))
    updated = 0

    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        while True:
            rows = conn.execute(
                """
                SELECT id, chunk_text
                FROM public.risk_entity_chunks
                WHERE chunk_text_normalized IS NULL OR trim(chunk_text_normalized) = ''
                ORDER BY created_at
                LIMIT %s
                """,
                (batch_size,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                stored, norm = prepare_chunk_text_for_storage(str(row["chunk_text"] or ""))
                conn.execute(
                    """
                    UPDATE public.risk_entity_chunks
                    SET chunk_text = %s, chunk_text_normalized = %s
                    WHERE id = %s
                    """,
                    (stored, norm or None, row["id"]),
                )
                updated += 1
            conn.commit()
            print(f"updated {updated} rows...", flush=True)

    print(f"done: {updated} chunks normalized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
