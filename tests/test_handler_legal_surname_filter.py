"""CJF search drops person mentions whose full name carries a different surname."""

import json
import sys
import types
from unittest import mock

sys.modules.setdefault(
    "nuwa_api_auth",
    types.SimpleNamespace(jwt_allows_client=lambda *a: True, require_jwt=lambda e: {}),
)

import handler_legal  # noqa: E402

JOEL_ROWS = [
    {"mention_id": i, "documento_id": f"d{i}", "nombre": n, "tipo": "persona", "score": 0.6}
    for i, n in enumerate(
        [
            "Joel Hernández Hernández",
            "Joel López Hernández",
            "Joel Hernández Sosa",
            "Joel Hernández Verdugo",
            "Joel Hernández Mendoza",
            "Joel Humberto Hernández Gómez",
            "Compareció Carlos Joel Ramírez Hernández",
            "Joel Hernández",
            "Joel Hernandes Hernández",
        ]
    )
]


def _search(body: dict, rows: list[dict]) -> tuple[dict, mock.Mock]:
    fake = mock.Mock(return_value=rows)
    with mock.patch("nuwa_legal_pg.search_cjf_mentions_pg", fake):
        res = handler_legal._handle_search(body)
    return json.loads(res["body"]), fake


def test_joel_keeps_only_compatible_names() -> None:
    out, fake = _search({"query": "Joel Hernández Hernández", "entityType": "individual", "limit": 25}, JOEL_ROWS)
    assert [h["nombre"] for h in out["hits"]] == [
        "Joel Hernández Hernández",
        "Joel Hernández",
        "Joel Hernandes Hernández",
    ]
    assert out["excludedSurnameConflicts"] == 6
    assert fake.call_args.kwargs["limit"] == 100


def test_trims_to_requested_limit_after_filtering() -> None:
    out, fake = _search({"query": "Joel Hernández Hernández", "entityType": "individual", "limit": 2}, JOEL_ROWS)
    assert len(out["hits"]) == 2
    assert fake.call_args.kwargs["limit"] == 8


def test_companies_and_short_queries_are_not_filtered() -> None:
    out, _ = _search({"query": "Joel Hernández Hernández", "entityType": "organization"}, JOEL_ROWS)
    assert out["excludedSurnameConflicts"] == 0
    assert len(out["hits"]) == len(JOEL_ROWS)
    out, fake = _search({"query": "Joel Hernández", "entityType": "individual", "limit": 25}, JOEL_ROWS)
    assert out["excludedSurnameConflicts"] == 0
    assert fake.call_args.kwargs["limit"] == 25
