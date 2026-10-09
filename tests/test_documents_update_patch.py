"""documents/update: extractedJsonPatch merges relations / documentRisk into extracted_json."""

import sys
from unittest import mock

import pytest

sys.modules.setdefault("boto3", mock.MagicMock())
botocore = mock.MagicMock()
sys.modules.setdefault("botocore", botocore)
sys.modules.setdefault("botocore.exceptions", botocore.exceptions)

import nuwa_documents_pg as docs
from nuwa_errors import SupabaseRestError

RELATIONS = [
    {"name": "MANIFIESTO", "partyType": "organization", "role": "Proveedor", "relatedTo": "SADAH CONTROL", "basis": "explicit"}
]


def test_patch_accepts_allowed_keys() -> None:
    patch = {"relations": RELATIONS, "documentRisk": {"level": "none"}, "relationsSource": "backfill"}
    assert docs._extracted_json_patch(patch) == patch


def test_patch_empty_when_missing() -> None:
    assert docs._extracted_json_patch(None) == {}


@pytest.mark.parametrize(
    "raw",
    [
        "relations",
        {"parties": []},
        {"relations": RELATIONS, "_linksCreated": 3},
        {"relations": {"name": "x"}},
        {"documentRisk": "high"},
    ],
)
def test_patch_rejects_invalid(raw) -> None:
    with pytest.raises(SupabaseRestError) as exc:
        docs._extracted_json_patch(raw)
    assert exc.value.status == 400


def _run_update(body):
    conn = mock.MagicMock()
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = conn
    with mock.patch.object(docs, "_get_doc", return_value={"status": "ready"}), mock.patch.object(
        docs, "_conn", return_value=ctx
    ), mock.patch.object(docs, "documents_get_pg", return_value={"documentId": "d1"}):
        docs.documents_update_pg(body)
    return conn


def test_update_merges_patch_into_extracted_json() -> None:
    conn = _run_update(
        {"clientId": 1, "documentId": "d1", "extractedJsonPatch": {"relations": RELATIONS}}
    )
    sql, params = conn.execute.call_args_list[0].args
    assert "extracted_json = COALESCE(extracted_json, '{}'::jsonb) || %s" in sql
    assert params[0].obj == {"relations": RELATIONS}
    assert params[-2:] == ["d1", 1]
    conn.commit.assert_called_once()


def test_update_without_fields_still_rejected() -> None:
    with pytest.raises(SupabaseRestError):
        _run_update({"clientId": 1, "documentId": "d1", "extractedJsonPatch": {}})
