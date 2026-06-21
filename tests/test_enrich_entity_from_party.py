from unittest.mock import MagicMock, patch

from nuwa_entities_pg import enrich_entity_from_party_pg


@patch("nuwa_entities_pg._conn")
def test_enrich_fills_empty_rfc_and_address(mock_conn) -> None:
    conn = MagicMock()
    mock_conn.return_value.__enter__.return_value = conn
    conn.execute.return_value.fetchone.return_value = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "rfc": None,
        "curp": None,
        "country": None,
        "metadata": {},
    }

    changed = enrich_entity_from_party_pg(
        entity_id="550e8400-e29b-41d4-a716-446655440000",
        party={
            "rfc": "IDB230904E31",
            "address": "Calle SUR 77 No. 448",
            "phones": ["5551234567"],
            "emails": ["contacto@idbc.com"],
        },
        user_id=1,
    )

    assert changed is True
    update_call = conn.execute.call_args_list[-1]
    assert update_call[0][1][1] == "IDB230904E31"
    meta = update_call[0][1][4]
    meta_dict = meta.obj if hasattr(meta, "obj") else meta
    assert meta_dict["address"] == "Calle SUR 77 No. 448"
    assert meta_dict["phone"] == "5551234567"
    assert meta_dict["email"] == "contacto@idbc.com"
