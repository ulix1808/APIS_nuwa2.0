import pathlib
import subprocess
import sys

import yaml


def test_openapi_parses() -> None:
    p = pathlib.Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml"
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["openapi"].startswith("3.")
    assert "/v1/reports/get" in data["paths"]
    assert "/v1/audit/create" in data["paths"]
    assert "/v1/audit/list" in data["paths"]
    assert "/v1/escalations/list" in data["paths"]
    assert "/v1/escalations/save" in data["paths"]
    assert "/v1/escalations/resolve" in data["paths"]
    save_props = data["paths"]["/v1/escalations/save"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "createdByEmail" in save_props
    resolve_res = data["paths"]["/v1/escalations/resolve"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["resolution"]["properties"]
    assert "resolvedByEmail" in resolve_res
    assert "/v1/clients/list" in data["paths"]
    assert "/v1/tokens/balance" in data["paths"]
    assert "/v1/tokens/ledger" in data["paths"]
    assert "/v1/tokens/consume" in data["paths"]
    assert "operatingCountries" in data["components"]["schemas"]["PlatformClient"]["properties"]
    audit_create = data["paths"]["/v1/audit/create"]["post"]["description"]
    assert "x-monitoring-worker-secret" in audit_create
    assert "/v1/admin/users/invite" in data["paths"]
    assert "/v1/admin/users/reset-password" in data["paths"]
    entity = data["components"]["schemas"]["EntitySummary"]["properties"]
    assert "lastReportFolio" in entity
    monitoring_list = (
        data["components"]["schemas"]["EntityMonitoringListResponse"]["properties"]["items"]["items"]["allOf"][1]["properties"]
    )
    assert "lastError" in monitoring_list
    reports_get = data["paths"]["/v1/reports/get"]["get"]["description"]
    assert "x-monitoring-worker-secret" in reports_get
    reports_save = data["paths"]["/v1/reports/save"]["post"]["description"]
    assert "x-monitoring-worker-secret" in reports_save
    alert_enum = data["paths"]["/v1/entities/alerts/create"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["alertType"]["enum"]
    assert "run_failed" in alert_enum
    finish_desc = data["paths"]["/v1/entities/monitoring/run-finish"]["post"]["description"]
    assert "2 h" in finish_desc or "2h" in finish_desc
    assert "/v1/source-category-id/create" in data["paths"]
    assert "/v1/source-category-id/list" in data["paths"]
    assert "/v1/source-category-id/{id}" in data["paths"]
    expires = data["components"]["schemas"]["LoginResponse"]["properties"]["expiresIn"]["description"]
    assert "259200" in expires


def test_openapi_no_duplicate_yaml_keys() -> None:
    """Swagger UI / swagger-cli fallan si hay claves YAML duplicadas (p. ej. description dos veces)."""
    p = pathlib.Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml"
    proc = subprocess.run(
        ["npx", "--yes", "@apidevtools/swagger-cli", "validate", str(p)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
    assert proc.returncode == 0, proc.stderr or proc.stdout
