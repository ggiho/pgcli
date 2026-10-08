from pgcli.userinfo import _group_privileges, list_users
from utils import dbtest


def test_group_privileges_one_row_per_object_in_type_order():
    rows = [
        ("table", "public", "t", "SELECT"),
        ("schema", "public", None, "USAGE"),
        ("table", "public", "t", "INSERT"),
        ("database", None, "db", "CONNECT"),
    ]
    assert _group_privileges(rows) == [
        ("database", None, "db", "CONNECT"),
        ("schema", "public", None, "USAGE"),
        ("table", "public", "t", "INSERT, SELECT"),
    ]


@dbtest
def test_u_without_argument_lists_roles(connection):
    with connection.cursor() as cur:
        [(_, rows, headers, _)] = list_users(cur, "", False)
        assert "rolname" in headers
        assert any(row[0] == "postgres" for row in rows)


@dbtest
def test_u_unknown_role(connection):
    with connection.cursor() as cur:
        assert list_users(cur, "_no_such_role", False) == [(None, None, None, "No such user: _no_such_role")]


@dbtest
def test_u_shows_attributes_and_direct_privileges(connection):
    with connection.cursor() as cur:
        cur.execute("DROP ROLE IF EXISTS _u_test_reader")
        cur.execute("CREATE ROLE _u_test_reader LOGIN")
        try:
            cur.execute("CREATE TABLE _u_test_t (a int)")
            cur.execute("GRANT SELECT, INSERT ON _u_test_t TO _u_test_reader")
            cur.execute("GRANT USAGE ON SCHEMA public TO _u_test_reader")

            role, privileges = list_users(cur, "'_u_test_reader'", False)

            _, rows, headers, _ = role
            assert "oid" not in headers
            assert dict(zip(headers, rows[0]))["login"] is True

            _, rows, headers, status = privileges
            assert headers == ["type", "schema", "object", "privileges"]
            assert ("schema", "public", None, "USAGE") in rows
            assert ("table", "public", "_u_test_t", "INSERT, SELECT") in rows
            assert status.startswith(f"{len(rows)} objects")

            # The table's owner holds every privilege on it, but through ownership, not a grant.
            _, (_, rows, _, _) = list_users(cur, "postgres", False)
            assert not any(row[2] == "_u_test_t" for row in rows)
        finally:
            cur.execute("DROP TABLE IF EXISTS _u_test_t")
            cur.execute("DROP OWNED BY _u_test_reader")
            cur.execute("DROP ROLE _u_test_reader")
