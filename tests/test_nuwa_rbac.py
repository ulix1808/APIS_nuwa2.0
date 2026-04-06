import pytest

from nuwa_rbac import can_read_report, reports_list_query_parts


def _actor(slug: str, cid: int, uid: int) -> dict:
    return {"role_slug": slug, "client_id": cid, "id": uid}


def _rep(cid: int, author: int) -> dict:
    return {"client_id": cid, "created_by_user_id": author}


def test_super_admin_reads_any() -> None:
    a = _actor("super_admin", 1, 1)
    assert can_read_report(a, _rep(99, 42))


def test_admin_reads_company_not_other() -> None:
    a = _actor("admin", 10, 2)
    assert can_read_report(a, _rep(10, 3))
    assert not can_read_report(a, _rep(11, 3))


def test_user_reads_own_only() -> None:
    a = _actor("user", 10, 5)
    assert can_read_report(a, _rep(10, 5))
    assert not can_read_report(a, _rep(10, 6))


def test_list_parts_super_admin_optional_filters() -> None:
    a = _actor("super_admin", 1, 1)
    p = reports_list_query_parts(a, filter_client_id=None, filter_created_by_user_id=None)
    assert "status=neq.deleted" in p


def test_list_parts_user_cannot_filter_other_creator() -> None:
    a = _actor("user", 10, 5)
    with pytest.raises(PermissionError):
        reports_list_query_parts(
            a, filter_client_id=10, filter_created_by_user_id=99
        )
