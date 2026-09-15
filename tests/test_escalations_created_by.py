"""Unit tests — escalations row mapping (createdBy* fields)."""

from __future__ import annotations

import os
import sys
from types import ModuleType


def _install_stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_pg():
    for name in (
        "psycopg",
        "psycopg.errors",
        "psycopg.types",
        "psycopg.types.json",
    ):
        if name not in sys.modules:
            _install_stub(name)
    sys.modules["psycopg.types.json"] = _install_stub("psycopg.types.json", Json=lambda x: x)
    _install_stub(
        "nuwa_errors",
        SupabaseRestError=type(
            "SupabaseRestError",
            (Exception,),
            {
                "__init__": lambda self, status, body: (
                    setattr(self, "status", status),
                    setattr(self, "body", body),
                    None,
                )[-1]
            },
        ),
    )
    _install_stub("nuwa_pg_dispatch", _conn=lambda: None)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cdk", "lambdas"))
    sys.modules.pop("nuwa_escalations_pg", None)
    import nuwa_escalations_pg as mod

    return mod


def test_row_to_escalation_includes_created_by() -> None:
    mod = _load_pg()
    out = mod.row_to_escalation(
        {
            "id": "esc-1",
            "client_id": 7,
            "entity_name": "ACME",
            "entity_id": None,
            "client_name": None,
            "context": "screening",
            "risk_level": "high",
            "actions": [],
            "notes": "",
            "priority": "urgent",
            "status": "active",
            "quick_resolve": False,
            "report_id": None,
            "notify_email": None,
            "action_path": None,
            "created_by_user_id": "6",
            "created_by_name": "Ana",
            "created_by_email": "ana@example.com",
            "created_at": None,
            "resolved_at": None,
            "resolution": None,
        }
    )
    assert out["createdByUserId"] == "6"
    assert out["createdByName"] == "Ana"
    assert out["createdByEmail"] == "ana@example.com"
