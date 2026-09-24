from report_helpers import db_row_to_api_summary, group_fields_from_row


def test_group_name_comes_from_column():
    fields = group_fields_from_row(
        {
            "group_id": "GRP-ABC",
            "group_name": "A. Proveedores",
            "group_role": "member",
        }
    )
    assert fields["groupName"] == "A. Proveedores"
    assert fields["groupId"] == "GRP-ABC"
    assert fields["groupRole"] == "member"


def test_group_name_falls_back_to_report_json_when_column_empty():
    fields = group_fields_from_row(
        {
            "group_id": None,
            "group_name": None,
            "report_json": {
                "metadatos": {"groupId": "GRP-ABC", "groupName": "A. Proveedores"},
            },
        }
    )
    assert fields["groupName"] == "A. Proveedores"
    assert fields["groupId"] == "GRP-ABC"


def test_list_summary_includes_group_name():
    summary = db_row_to_api_summary(
        {
            "folio": "GRP-ABC-1",
            "client_id": 1,
            "created_by_user_id": 2,
            "entidad": "TD Synnex Corp",
            "status": "active",
            "group_name": "A. Proveedores",
            "group_id": "GRP-ABC",
        }
    )
    assert summary["groupName"] == "A. Proveedores"
    assert summary["folio"] == "GRP-ABC-1"
    assert "report_json" not in summary
