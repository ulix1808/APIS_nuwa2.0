"""'Otro identificador' of an entity (foreign IDs used by the early-warning match)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cdk" / "lambdas"))

import nuwa_entities_pg as ents  # noqa: E402


def test_normalize_other_ids():
    assert ents.normalize_other_ids(
        [
            {"type": "cedula", "value": "79.876.543"},
            {"type": "CEDULA", "value": "79876543"},  # same one
            {"type": "pasaporte", "value": "g-1234567"},  # unknown type → other
            {"type": "passport", "value": "AB"},  # too short
            "x",
        ]
    ) == [{"type": "cedula", "value": "79876543"}, {"type": "other", "value": "G1234567"}]
    assert ents.normalize_other_ids(None) == []
    assert len(ents.normalize_other_ids([{"type": "other", "value": f"ID{i:06d}"} for i in range(9)])) == ents.MAX_OTHER_IDS


def test_resolved_fields_and_api_shape_carry_other_ids():
    fields = ents._resolve_entity_fields(
        {"partyType": "individual", "firstName": "Juan", "lastName": "Pérez", "otherIds": [{"type": "cedula", "value": "123456"}]}
    )
    assert fields["other_ids"] == [{"type": "cedula", "value": "123456"}]
