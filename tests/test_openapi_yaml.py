import pathlib

import yaml


def test_openapi_parses() -> None:
    p = pathlib.Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml"
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["openapi"].startswith("3.")
    assert "/v1/reports/get" in data["paths"]
