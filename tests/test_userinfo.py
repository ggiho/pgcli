import pytest

from pgcli.userinfo import _group_privileges, _role_name, list_users
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


@pytest.mark.parametrize(
    ("arg", "name"),
    [
        ("Role_Finance", "role_finance"),
        (" ROLE_FINANCE ", "role_finance"),
        ('"Role_Finance"', "Role_Finance"),
        ('"say ""hi"""', 'say "hi"'),
        ("'_u_reader'", "_u_reader"),
        ("'\"Mixed\"'", "Mixed"),
        # PostgreSQL folds only ASCII letters.
        ("ÉTÉ", "ÉtÉ"),
        ("", ""),
    ],
)
def test_role_name_is_read_like_an_identifier(arg, name):
    assert _role_name(arg) == name


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


@dbtest
def test_u_shows_column_function_and_type_grants(connection):
    with connection.cursor() as cur:
        cur.execute("DROP ROLE IF EXISTS _u_test_reader")
        cur.execute("CREATE ROLE _u_test_reader")
        try:
            cur.execute("CREATE TABLE _u_test_t (a int, b int)")
            cur.execute("GRANT SELECT (a), UPDATE (a) ON _u_test_t TO _u_test_reader")
            cur.execute("CREATE FUNCTION _u_test_f(n int) RETURNS int LANGUAGE sql AS 'SELECT n'")
            cur.execute("GRANT EXECUTE ON FUNCTION _u_test_f(int) TO _u_test_reader")
            cur.execute("CREATE PROCEDURE _u_test_p() LANGUAGE sql AS 'SELECT 1'")
            cur.execute("GRANT EXECUTE ON PROCEDURE _u_test_p() TO _u_test_reader")
            cur.execute("CREATE TYPE _u_test_ty AS ENUM ('x')")
            cur.execute("GRANT USAGE ON TYPE _u_test_ty TO _u_test_reader")

            _, (_, rows, _, _) = list_users(cur, "_u_test_reader", False)
            assert ("column", "public", "_u_test_t.a", "SELECT, UPDATE") in rows
            assert not any(row[2] == "_u_test_t.b" for row in rows)
            assert ("function", "public", "_u_test_f(n integer)", "EXECUTE") in rows
            assert ("procedure", "public", "_u_test_p()", "EXECUTE") in rows
            assert ("type", "public", "_u_test_ty", "USAGE") in rows

            # The owner's own entries are ownership, not grants.
            _, (_, rows, _, _) = list_users(cur, "postgres", False)
            assert not any(row[2] and row[2].startswith("_u_test_") for row in rows)
        finally:
            cur.execute("DROP TABLE IF EXISTS _u_test_t")
            cur.execute("DROP FUNCTION IF EXISTS _u_test_f(int)")
            cur.execute("DROP PROCEDURE IF EXISTS _u_test_p()")
            cur.execute("DROP TYPE IF EXISTS _u_test_ty")
            cur.execute("DROP OWNED BY _u_test_reader")
            cur.execute("DROP ROLE _u_test_reader")


@dbtest
def test_u_folds_unquoted_names_and_keeps_quoted_ones(connection):
    with connection.cursor() as cur:
        cur.execute('DROP ROLE IF EXISTS _u_test_lower, "_U_Test_Mixed"')
        cur.execute("CREATE ROLE _u_test_lower")
        cur.execute('CREATE ROLE "_U_Test_Mixed"')
        try:
            [role, _] = list_users(cur, "_U_TEST_LOWER", False)
            assert role[1][0][0] == "_u_test_lower"

            [role, _] = list_users(cur, '"_U_Test_Mixed"', False)
            assert role[1][0][0] == "_U_Test_Mixed"

            assert list_users(cur, "_U_Test_Mixed", False) == [(None, None, None, "No such user: _u_test_mixed")]
        finally:
            cur.execute('DROP ROLE _u_test_lower, "_U_Test_Mixed"')


@dbtest
def test_u_plus_adds_the_description(connection):
    with connection.cursor() as cur:
        cur.execute("DROP ROLE IF EXISTS _u_test_described")
        cur.execute("CREATE ROLE _u_test_described")
        try:
            cur.execute("COMMENT ON ROLE _u_test_described IS 'reporting'")

            [(_, rows, headers, _), _] = list_users(cur, "_u_test_described", False)
            assert "description" not in headers

            [(_, rows, headers, _), _] = list_users(cur, "_u_test_described", True)
            assert dict(zip(headers, rows[0]))["description"] == "reporting"
        finally:
            cur.execute("DROP ROLE _u_test_described")
