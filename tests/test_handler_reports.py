import json
from unittest import mock

import handler_reports
from nuwa_config import SupabaseConfigError


def test_get_requires_one_filter() -> None:
    event = {
        "httpMethod": "GET",
        "path": "/prod/v1/reports/get",
        "queryStringParameters": {},
    }
    with mock.patch(
        "nuwa_config.get_supabase_config",
        return_value={"url": "https://x.supabase.co", "service_role_key": "k"},
    ):
        out = handler_reports.handler(event, None)
    assert out["statusCode"] == 400
    body = json.loads(out["body"])
    assert "clientId" in body["message"] or "al menos" in body["message"]


@mock.patch("nuwa_config.get_supabase_config", side_effect=SupabaseConfigError("x"))
def test_supabase_not_configured(mock_cfg) -> None:
    event = {"httpMethod": "GET", "path": "/v1/reports/get", "queryStringParameters": {}}
    out = handler_reports.handler(event, None)
    assert out["statusCode"] == 503


def test_save_sync_relations_never_breaks_the_report() -> None:
    import nuwa_relations_pg

    with mock.patch.object(handler_reports, "is_database_mode", return_value=True), mock.patch.object(
        nuwa_relations_pg, "replace_report_relations", side_effect=RuntimeError("db down")
    ) as rep:
        handler_reports._sync_relations_safely(
            {"entityRelations": [{"subjectName": "A"}]}, 7, "SE-1", entity_id="e-1", report_created_at="t"
        )
    assert rep.call_args.args[:2] == (7, "SE-1")
    assert rep.call_args.kwargs["subject_entity_id"] == "e-1"


def test_save_without_relations_skips_sync() -> None:
    import nuwa_relations_pg

    with mock.patch.object(handler_reports, "is_database_mode", return_value=True), mock.patch.object(
        nuwa_relations_pg, "replace_report_relations"
    ) as rep:
        handler_reports._sync_relations_safely({}, 7, "SE-1")
    rep.assert_not_called()
