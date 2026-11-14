import json
from unittest import mock

import handler_documents


def _event(path_suffix: str, body: dict) -> dict:
    return {
        "httpMethod": "POST",
        "path": f"/prod/v1/documents/{path_suffix}",
        "body": json.dumps(body),
        "headers": {"Authorization": "Bearer tok"},
    }


def _worker_event(path_suffix: str, body: dict, secret: str = "worker-secret") -> dict:
    return {
        "httpMethod": "POST",
        "path": f"/prod/v1/documents/{path_suffix}",
        "body": json.dumps(body),
        "headers": {"x-monitoring-worker-secret": secret},
    }


_DOC_BODY = {
    "clientId": 8,
    "userId": 21,
    "filename": "acta.pdf",
    "mimeType": "application/pdf",
    "fileSizeBytes": 1024,
    "category": "proveedor",
}


@mock.patch("handler_documents.is_database_mode", return_value=True)
@mock.patch("handler_documents.ensure_data_backend")
@mock.patch(
    "nuwa_documents_pg.documents_presign_pg",
    return_value={"success": True, "documentId": "d-1", "uploadUrl": "https://s3.example/put"},
)
def test_presign_accepts_worker_secret(mock_presign, _ensure, _pg) -> None:
    with mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"}):
        out = handler_documents.handler(_worker_event("presign", _DOC_BODY), None)
    assert out["statusCode"] == 200
    assert mock_presign.call_args[0][0]["clientId"] == 8


@mock.patch("handler_documents.is_database_mode", return_value=True)
@mock.patch("handler_documents.ensure_data_backend")
@mock.patch(
    "nuwa_documents_pg.documents_upload_complete_pg",
    return_value={"success": True, "documentId": "d-1", "status": "uploaded"},
)
def test_upload_complete_accepts_worker_secret(mock_complete, _ensure, _pg) -> None:
    body = {"clientId": 8, "userId": 21, "documentId": "d-1"}
    with mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"}):
        out = handler_documents.handler(_worker_event("upload-complete", body), None)
    assert out["statusCode"] == 200
    assert mock_complete.call_args[0][0]["documentId"] == "d-1"


@mock.patch("handler_documents.is_database_mode", return_value=True)
@mock.patch("handler_documents.ensure_data_backend")
@mock.patch(
    "nuwa_documents_pg.documents_finalize_pg",
    return_value={"success": True, "documentId": "d-1", "status": "ready", "primaryEntityId": "e-1"},
)
def test_finalize_accepts_worker_secret(mock_finalize, _ensure, _pg) -> None:
    body = {
        "clientId": 8,
        "userId": 21,
        "documentId": "d-1",
        "primaryEntityId": "e-1",
        "primaryEntityRole": "Proveedor",
        "extractedJson": {"summary": "ok"},
        "autoCreateEntities": False,
        "autoIndex": False,
    }
    with mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"}):
        out = handler_documents.handler(_worker_event("finalize", body), None)
    assert out["statusCode"] == 200
    assert mock_finalize.call_args[0][0]["primaryEntityId"] == "e-1"


@mock.patch("handler_documents.is_database_mode", return_value=True)
@mock.patch("handler_documents.ensure_data_backend")
@mock.patch("nuwa_documents_pg.documents_presign_pg")
def test_presign_rejects_wrong_or_missing_worker_secret(mock_presign, _ensure, _pg) -> None:
    with mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"}):
        with mock.patch("handler_documents.require_jwt", return_value="Token requerido"):
            out = handler_documents.handler(_worker_event("presign", _DOC_BODY, secret="otro"), None)
        assert out["statusCode"] == 401
        no_client = {k: v for k, v in _DOC_BODY.items() if k != "clientId"}
        out = handler_documents.handler(_worker_event("presign", no_client), None)
        assert out["statusCode"] == 400
    mock_presign.assert_not_called()


@mock.patch("handler_documents.is_database_mode", return_value=True)
@mock.patch("handler_documents.ensure_data_backend")
@mock.patch("nuwa_documents_pg.documents_list_pg")
@mock.patch("nuwa_documents_pg.documents_get_pg")
@mock.patch("nuwa_documents_pg.documents_delete_pg")
@mock.patch("nuwa_documents_pg.documents_download_url_pg")
def test_worker_secret_cannot_list_get_delete_or_download(
    mock_dl, mock_delete, mock_get, mock_list, _ensure, _pg
) -> None:
    body = {"clientId": 8, "userId": 21, "documentId": "d-1"}
    with mock.patch.dict("os.environ", {"MONITORING_WORKER_SECRET": "worker-secret"}):
        with mock.patch("handler_documents.require_jwt", return_value="Token requerido"):
            for suffix in ("list", "get", "delete", "download-url"):
                out = handler_documents.handler(_worker_event(suffix, body), None)
                assert out["statusCode"] == 401, suffix
    mock_list.assert_not_called()
    mock_get.assert_not_called()
    mock_delete.assert_not_called()
    mock_dl.assert_not_called()


@mock.patch("handler_documents.is_database_mode", return_value=True)
@mock.patch("handler_documents.ensure_data_backend")
@mock.patch(
    "nuwa_documents_pg.documents_presign_pg",
    return_value={"success": True, "documentId": "d-1", "uploadUrl": "https://s3.example/put"},
)
def test_presign_still_accepts_jwt_user(mock_presign, _ensure, _pg) -> None:
    with mock.patch(
        "handler_documents.require_jwt",
        return_value={"sub": 21, "cid": 8, "role": "admin"},
    ):
        with mock.patch("handler_documents.jwt_matches_actor_body", return_value=True):
            with mock.patch("handler_documents.jwt_allows_client", return_value=True):
                with mock.patch("handler_documents.effective_tenant_scope", return_value=None):
                    out = handler_documents.handler(_event("presign", _DOC_BODY), None)
    assert out["statusCode"] == 200
    assert mock_presign.called
