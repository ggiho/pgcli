from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.sql import SQL

from pgcli import userinfo
from pgcli.userinfo import _group_privileges, _pg_privileges_query, _role_name, list_users
from utils import dbtest


class FakeCursor:
    """Records the SQL it is given and answers with the canned rows of the first matching needle."""

    adapters = psycopg.adapters

    def __init__(self, responses, server_version=80002, transaction_status=TransactionStatus.IDLE):
        self.responses = responses
        self.connection = SimpleNamespace(
            info=SimpleNamespace(server_version=server_version, transaction_status=transaction_status), pgconn=None
        )
        self.sql = []
        self.description = None

    def __bool__(self):
        # Falsy, so psycopg renders SQL for it as it would without a connection.
        return False

    def execute(self, query):
        sql = query.as_string(None)
        self.sql.append(sql)
        for needle, (headers, rows) in self.responses.items():
            if needle in sql:
                self.description = [SimpleNamespace(name=h) for h in headers]
                self.rows = rows
                return
        raise AssertionError(f"unexpected query: {sql}")

    def fetchall(self):
        return self.rows


REDSHIFT_USER = (["rolname", "super", "createdb", "valid_until", "groups"], [("analyst", False, False, None, ["g1"])])
REDSHIFT_PRIVILEGES = (["type", "schema", "object", "privilege"], [("schema", "sales", None, "USAGE")])


def test_redshift_user_is_looked_up_folded_with_its_roles_and_user_privileges():
    cur = FakeCursor({
        "pg_catalog.pg_user": REDSHIFT_USER,
        "svv_user_grants": (["role_name"], [("r2",), ("r1",)]),
        "svv_database_privileges": REDSHIFT_PRIVILEGES,
    })

    role, privileges = list_users(cur, "Analyst", False)

    assert role == (None, [("analyst", False, False, None, ["g1"], ["r1", "r2"])], REDSHIFT_USER[0] + ["roles"], None)
    assert privileges[1] == [("schema", "sales", None, "USAGE")]
    assert "u.usename = 'analyst'" in cur.sql[0]
    assert "user_name = 'analyst'" in cur.sql[1]
    # identity_type keeps a user apart from a same-named role or group.
    assert cur.sql[2].count("identity_name = 'analyst' AND identity_type = 'user'") == 3
    assert not any("svv_roles" in sql for sql in cur.sql)


def test_redshift_role_that_is_not_a_user_gets_role_privileges():
    cur = FakeCursor({
        "pg_catalog.pg_user": (REDSHIFT_USER[0], []),
        "svv_roles": (["role_name"], [("Finance",)]),
        "svv_database_privileges": REDSHIFT_PRIVILEGES,
    })

    role, _ = list_users(cur, '"Finance"', False)

    assert role == (None, [("Finance", "role")], ["rolname", "type"], None)
    assert "role_name = 'Finance'" in cur.sql[1]
    assert cur.sql[2].count("identity_name = 'Finance' AND identity_type = 'role'") == 3


def test_redshift_name_that_is_neither_user_nor_role():
    cur = FakeCursor({"pg_catalog.pg_user": (REDSHIFT_USER[0], []), "svv_roles": (["role_name"], [])})

    assert list_users(cur, "nobody", False) == [(None, None, None, "No such user: nobody")]
    assert len(cur.sql) == 2


def test_redshift_skips_the_privilege_lookup_inside_a_transaction():
    """Redshift cannot ROLLBACK TO SAVEPOINT, so a failed lookup would abort the user's transaction."""
    cur = FakeCursor(
        {"pg_catalog.pg_user": REDSHIFT_USER, "svv_user_grants": (["role_name"], [])},
        transaction_status=TransactionStatus.INTRANS,
    )

    role, (_, rows, _, status) = list_users(cur, "analyst", False)

    assert rows is None and "inside a transaction" in status
    assert not any("svv_database_privileges" in sql for sql in cur.sql)


@pytest.mark.parametrize(
    ("server_version", "prokind", "parameter_acl"),
    [(100023, False, False), (110000, True, False), (140000, True, False), (150000, True, True), (160004, True, True)],
)
def test_privileges_query_only_references_catalogs_the_server_has(server_version, prokind, parameter_acl):
    sql = _pg_privileges_query(10, server_version).as_string(None)

    assert ("prokind" in sql) is prokind
    assert ("proisagg" in sql) is not prokind
    assert ("pg_parameter_acl" in sql) is parameter_acl
    assert "pg_largeobject_metadata" in sql


@pytest.mark.parametrize("arg", ['""', "''", "'\"\"'"])
def test_u_rejects_an_empty_name_instead_of_listing_all_roles(arg):
    cur = FakeCursor({})

    assert list_users(cur, arg, False) == [(None, None, None, f"No such user: {arg}")]
    assert cur.sql == []


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
            # Procedures arrived in PostgreSQL 11.
            has_procedures = connection.info.server_version >= 110000
            if has_procedures:
                cur.execute("CREATE PROCEDURE _u_test_p() LANGUAGE sql AS 'SELECT 1'")
                cur.execute("GRANT EXECUTE ON PROCEDURE _u_test_p() TO _u_test_reader")
            cur.execute("CREATE AGGREGATE _u_test_agg(int) (sfunc = int4pl, stype = int)")
            cur.execute("GRANT EXECUTE ON FUNCTION _u_test_agg(int) TO _u_test_reader")
            cur.execute("CREATE TYPE _u_test_ty AS ENUM ('x')")
            cur.execute("GRANT USAGE ON TYPE _u_test_ty TO _u_test_reader")

            _, (_, rows, _, _) = list_users(cur, "_u_test_reader", False)
            assert ("column", "public", "_u_test_t.a", "SELECT, UPDATE") in rows
            assert not any(row[2] == "_u_test_t.b" for row in rows)
            assert ("function", "public", "_u_test_f(n integer)", "EXECUTE") in rows
            assert (("procedure", "public", "_u_test_p()", "EXECUTE") in rows) is has_procedures
            assert ("aggregate", "public", "_u_test_agg(integer)", "EXECUTE") in rows
            assert ("type", "public", "_u_test_ty", "USAGE") in rows

            # The owner's own entries are ownership, not grants.
            _, (_, rows, _, _) = list_users(cur, "postgres", False)
            assert not any(row[2] and row[2].startswith("_u_test_") for row in rows)
        finally:
            cur.execute("DROP TABLE IF EXISTS _u_test_t")
            cur.execute("DROP FUNCTION IF EXISTS _u_test_f(int)")
            if connection.info.server_version >= 110000:
                cur.execute("DROP PROCEDURE IF EXISTS _u_test_p()")
            cur.execute("DROP AGGREGATE IF EXISTS _u_test_agg(int)")
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


@dbtest
def test_u_failed_lookup_inside_a_transaction_does_not_abort_it(connection, monkeypatch):
    monkeypatch.setattr(userinfo, "_pg_privileges_query", lambda oid, server_version: SQL("SELECT 1/0"))
    with connection.cursor() as cur:
        cur.execute("BEGIN")
        try:
            cur.execute("CREATE TEMP TABLE _u_test_kept (a int)")

            role, (_, _, _, status) = list_users(cur, "postgres", False)

            assert role[1][0][0] == "postgres"
            assert status.startswith("Cannot read privileges: division by zero")
            assert connection.info.transaction_status == TransactionStatus.INTRANS
            cur.execute("SELECT count(*) FROM _u_test_kept")
            assert cur.fetchone() == (0,)
        finally:
            cur.execute("ROLLBACK")


@dbtest
def test_u_works_inside_a_transaction(connection):
    with connection.cursor() as cur:
        cur.execute("BEGIN")
        try:
            [_, (_, rows, _, _)] = list_users(cur, "postgres", False)
            assert isinstance(rows, list)
            assert connection.info.transaction_status == TransactionStatus.INTRANS
        finally:
            cur.execute("ROLLBACK")


@dbtest
def test_u_shows_language_fdw_server_tablespace_large_object_and_parameter_grants(connection):
    server_version = connection.info.server_version
    with connection.cursor() as cur:
        cur.execute("DROP ROLE IF EXISTS _u_test_reader")
        cur.execute("CREATE ROLE _u_test_reader")
        cur.execute("CREATE FOREIGN DATA WRAPPER _u_test_fdw")
        cur.execute("CREATE SERVER _u_test_srv FOREIGN DATA WRAPPER _u_test_fdw")
        cur.execute("SELECT lo_create(0)")
        [lo] = cur.fetchone()
        try:
            cur.execute("GRANT USAGE ON LANGUAGE sql TO _u_test_reader")
            cur.execute("GRANT USAGE ON FOREIGN DATA WRAPPER _u_test_fdw TO _u_test_reader")
            cur.execute("GRANT USAGE ON FOREIGN SERVER _u_test_srv TO _u_test_reader")
            cur.execute("GRANT CREATE ON TABLESPACE pg_default TO _u_test_reader")
            cur.execute(f"GRANT SELECT, UPDATE ON LARGE OBJECT {lo} TO _u_test_reader")
            if server_version >= 150000:
                cur.execute("GRANT SET ON PARAMETER work_mem TO _u_test_reader")

            _, (_, rows, _, _) = list_users(cur, "_u_test_reader", False)
            assert ("language", None, "sql", "USAGE") in rows
            assert ("foreign-data wrapper", None, "_u_test_fdw", "USAGE") in rows
            assert ("foreign server", None, "_u_test_srv", "USAGE") in rows
            assert ("tablespace", None, "pg_default", "CREATE") in rows
            assert ("large object", None, str(lo), "SELECT, UPDATE") in rows
            if server_version >= 150000:
                assert ("parameter", None, "work_mem", "SET") in rows

            # The owner's own entries are ownership, not grants.
            _, (_, rows, _, _) = list_users(cur, "postgres", False)
            assert not any(row[2] in ("sql", "_u_test_fdw", "_u_test_srv", "pg_default", str(lo)) for row in rows)
        finally:
            if server_version >= 150000:
                cur.execute("REVOKE SET ON PARAMETER work_mem FROM _u_test_reader")
            cur.execute("REVOKE USAGE ON LANGUAGE sql FROM _u_test_reader")
            cur.execute("REVOKE CREATE ON TABLESPACE pg_default FROM _u_test_reader")
            cur.execute(f"SELECT lo_unlink({lo})")
            cur.execute("DROP SERVER _u_test_srv")
            cur.execute("DROP FOREIGN DATA WRAPPER _u_test_fdw")
            cur.execute("DROP OWNED BY _u_test_reader")
            cur.execute("DROP ROLE _u_test_reader")
