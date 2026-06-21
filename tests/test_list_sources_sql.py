"""SQL de list_sources no debe romper psycopg con % literales en LIKE."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cdk" / "lambdas"))

from nuwa_pg_dispatch import _sources_list_exclude_document_internal_sql


def test_exclude_document_sql_escapes_percent_for_psycopg() -> None:
    sql = _sources_list_exclude_document_internal_sql()
    assert "doc:%%" in sql
    assert "LIKE 'doc:%'" not in sql.replace("doc:%%", "")
