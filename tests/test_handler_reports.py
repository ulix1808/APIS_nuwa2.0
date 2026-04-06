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
