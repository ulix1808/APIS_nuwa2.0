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


@mock.patch("handler_entities.is_database_mode", return_value=False)
@mock.patch("handler_entities.ensure_data_backend")
def test_entities_requires_database_mode(_ensure, _pg) -> None:
    body = {"clientId": 1, "userId": 1, "partyType": "individual", "partyTypeLabel": "Persona física"}
    with mock.patch("handler_entities.require_jwt", return_value={"sub": 1, "cid": 1, "role": "admin"}):
        out = handler_entities.handler(_event("match", body), None)
    assert out["statusCode"] == 503


@mock.patch("handler_entities.is_database_mode", return_value=True)
@mock.patch("handler_entities.ensure_data_backend")
@mock.patch("nuwa_entities_pg.entities_match_pg", return_value={"matches": [], "hasStrongMatch": False})
def test_match_ok(mock_match, _ensure, _pg) -> None:
    body = {
        "clientId": 1,
        "userId": 1,
        "partyType": "organization",
        "partyTypeLabel": "Persona moral",
        "legalName": "ACME",
    }
    with mock.patch("handler_entities.require_jwt", return_value={"sub": 1, "cid": 1, "role": "admin"}):
        out = handler_entities.handler(_event("match", body), None)
    assert out["statusCode"] == 200
    assert json.loads(out["body"])["hasStrongMatch"] is False


def _worker_event(path_suffix: str, body: dict, secret: str = "worker-secret") -> dict:
    return {
        "httpMethod": "POST",
        "path": f"/prod/v1/entities/{path_suffix}",
        "body": json.dumps(body),
        "headers": {"x-monitoring-worker-secret": secret},
    }


_SUPPLIER = {
    "clientId": 8,
    "userId": 21,
    "partyType": "organization",
    "partyTypeLabel": "Persona moral",
    "legalName": "Servicios Ana SA de CV",
    "rfc": "SAN010101AB1",
}


@mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"})
@mock.patch("handler_entities.is_database_mode", return_value=True)
@mock.patch("handler_entities.ensure_data_backend")
@mock.patch("nuwa_entities_pg.entities_create_pg", return_value={"entity": {"entityId": "e-1"}})
def test_create_accepts_worker_secret_for_suppliers(mock_create, _ensure, _pg) -> None:
    out = handler_entities.handler(_worker_event("create", _SUPPLIER), None)
    assert out["statusCode"] == 201
    assert mock_create.call_args[0][0]["clientId"] == 8


@mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"})
@mock.patch("handler_entities.is_database_mode", return_value=True)
@mock.patch("handler_entities.ensure_data_backend")
@mock.patch("nuwa_entities_pg.entities_create_pg")
def test_create_rejects_wrong_worker_secret_or_missing_client(mock_create, _ensure, _pg) -> None:
    with mock.patch("handler_entities.require_jwt", return_value="Token requerido"):
        out = handler_entities.handler(_worker_event("create", _SUPPLIER, secret="otro"), None)
    assert out["statusCode"] == 401
    no_client = {k: v for k, v in _SUPPLIER.items() if k != "clientId"}
    out = handler_entities.handler(_worker_event("create", no_client), None)
    assert out["statusCode"] == 400
    mock_create.assert_not_called()


@mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"})
@mock.patch("handler_entities.is_database_mode", return_value=True)
@mock.patch("handler_entities.ensure_data_backend")
@mock.patch("nuwa_entities_pg.entities_update_pg")
def test_worker_secret_still_cannot_update_entities(mock_update, _ensure, _pg) -> None:
    with mock.patch("handler_entities.require_jwt", return_value="Token requerido"):
        out = handler_entities.handler(_worker_event("update", {"clientId": 8, "userId": 21, "entityId": "e-1"}), None)
    assert out["statusCode"] == 401
    mock_update.assert_not_called()
