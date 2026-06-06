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
    assert "/v1/source-category-id/create" in data["paths"]
    assert "/v1/source-category-id/list" in data["paths"]
    assert "/v1/source-category-id/{id}" in data["paths"]


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
