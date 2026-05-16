import json
from unittest import mock

import handler_entities


def _event(path_suffix: str, body: dict) -> dict:
    return {
        "httpMethod": "POST",
        "path": f"/prod/v1/entities/{path_suffix}",
        "body": json.dumps(body),
        "headers": {"Authorization": "Bearer tok"},
    }


@mock.patch("nuwa_config.is_database_mode", return_value=False)
@mock.patch("nuwa_config.ensure_data_backend")
def test_entities_requires_database_mode(_ensure, _pg) -> None:
    body = {"clientId": 1, "userId": 1, "partyType": "individual", "partyTypeLabel": "Persona física"}
    with mock.patch("nuwa_api_auth.require_jwt", return_value={"sub": 1, "cid": 1, "role": "admin"}):
        out = handler_entities.handler(_event("match", body), None)
    assert out["statusCode"] == 503


@mock.patch("nuwa_config.is_database_mode", return_value=True)
@mock.patch("nuwa_config.ensure_data_backend")
@mock.patch("nuwa_entities_pg.entities_match_pg", return_value={"matches": [], "hasStrongMatch": False})
def test_match_ok(mock_match, _ensure, _pg) -> None:
    body = {
        "clientId": 1,
        "userId": 1,
        "partyType": "organization",
        "partyTypeLabel": "Persona moral",
        "legalName": "ACME",
    }
    with mock.patch("nuwa_api_auth.require_jwt", return_value={"sub": 1, "cid": 1, "role": "admin"}):
        out = handler_entities.handler(_event("match", body), None)
    assert out["statusCode"] == 200
    assert json.loads(out["body"])["hasStrongMatch"] is False
