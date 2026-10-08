"""``\\u``: list roles, or show one role's attributes and the privileges granted to it.

Mirrors the fork's mycli ``\\u``. With no argument it is pgspecial's ``\\du``.
With a name it looks the role up by exact name, not a ``\\du`` pattern, and also
lists the privileges granted to it directly, one row per object. An owner's
entry in its own object's ACL is left out, since that is ownership, not a grant.

Redshift is detected the way pgspecial does it, by its 8.0.2 server version. It
has neither ``aclexplode`` nor a usable ``pg_roles``, so it reads users from
``pg_user`` and privileges from the ``svv_*_privileges`` views. Those views
cannot be aggregated server-side: anything that sends them to the compute nodes
(``LISTAGG``, ``GROUP BY``) fails with "Insufficient privilege to run PG function
pg_user_has_admin_option", so privileges are grouped per object here instead.
"""

import logging

import psycopg
from psycopg.sql import SQL, Literal
from pgspecial.dbcommands import list_roles

_logger = logging.getLogger(__name__)

_PG_ROLE_QUERY = SQL(
    """
    SELECT r.oid, r.rolname, r.rolsuper AS super, r.rolinherit AS inherit,
        r.rolcreaterole AS createrole, r.rolcreatedb AS createdb,
        r.rolcanlogin AS login, r.rolconnlimit AS connlimit,
        r.rolvaliduntil AS valid_until,
        ARRAY(SELECT b.rolname FROM pg_catalog.pg_auth_members m
              JOIN pg_catalog.pg_roles b ON m.roleid = b.oid
              WHERE m.member = r.oid ORDER BY 1) AS member_of
    FROM pg_catalog.pg_roles r
    WHERE r.rolname = {name}
    """
)

_PG_PRIVILEGES_QUERY = SQL(
    """
    SELECT 'database', NULL, d.datname, a.privilege_type
    FROM pg_catalog.pg_database d, aclexplode(d.datacl) a
    WHERE a.grantee = {oid} AND a.grantee <> d.datdba
    UNION ALL
    SELECT 'schema', n.nspname, NULL, a.privilege_type
    FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
    WHERE a.grantee = {oid} AND a.grantee <> n.nspowner
    UNION ALL
    SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view'
                          WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table'
                          ELSE 'table' END,
        n.nspname, c.relname, a.privilege_type
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
        aclexplode(c.relacl) a
    WHERE a.grantee = {oid} AND a.grantee <> c.relowner
    """
)

_REDSHIFT_USER_QUERY = SQL(
    """
    SELECT u.usename AS rolname, u.usesuper AS super, u.usecreatedb AS createdb,
        u.valuntil AS valid_until,
        ARRAY(SELECT g.groname FROM pg_catalog.pg_group g
              WHERE u.usesysid = ANY(g.grolist)) AS groups
    FROM pg_catalog.pg_user u
    WHERE u.usename = {name}
    """
)

_REDSHIFT_PRIVILEGES_QUERY = SQL(
    """
    SELECT 'database', NULL, database_name, privilege_type
    FROM svv_database_privileges WHERE identity_name = {name}
    UNION ALL
    SELECT 'schema', namespace_name, NULL, privilege_type
    FROM svv_schema_privileges WHERE identity_name = {name}
    UNION ALL
    SELECT 'relation', namespace_name, relation_name, privilege_type
    FROM svv_relation_privileges WHERE identity_name = {name}
    """
)

_TYPE_ORDER = {"database": 0, "schema": 1}


def _is_redshift(cur):
    return cur.connection.info.server_version < 90000


def _fetch(cur, query):
    _logger.debug(query.as_string(cur))
    cur.execute(query)
    headers = [d.name for d in cur.description] if cur.description else None
    return headers, cur.fetchall()


def _group_privileges(rows):
    """Collapse (type, schema, object, privilege) rows into one row per object."""
    grouped = {}
    for obj_type, schema, obj, privilege in rows:
        grouped.setdefault((obj_type, schema, obj), set()).add(privilege)
    keys = sorted(grouped, key=lambda k: (_TYPE_ORDER.get(k[0], 2), k[1] or "", k[2] or ""))
    return [(*key, ", ".join(sorted(grouped[key]))) for key in keys]


def _pg_role(cur, name):
    headers, rows = _fetch(cur, _PG_ROLE_QUERY.format(name=Literal(name)))
    if not rows:
        return None, None
    oid = rows[0][0]
    # The oid is only needed for the privilege lookup, so it is not shown.
    return (headers[1:], [row[1:] for row in rows]), _PG_PRIVILEGES_QUERY.format(oid=Literal(oid))


def _redshift_role(cur, name):
    headers, rows = _fetch(cur, _REDSHIFT_USER_QUERY.format(name=Literal(name)))
    if rows:
        _, roles = _fetch(cur, SQL("SELECT role_name FROM svv_user_grants WHERE user_name = {name}").format(name=Literal(name)))
        headers = headers + ["roles"]
        rows = [(*rows[0], sorted(r[0] for r in roles))]
    else:
        # RBAC roles are not in pg_user, but can be granted privileges all the same.
        _, roles = _fetch(cur, SQL("SELECT role_name FROM svv_roles WHERE role_name = {name}").format(name=Literal(name)))
        if not roles:
            return None, None
        headers, rows = ["rolname", "type"], [(name, "role")]
    return (headers, rows), _REDSHIFT_PRIVILEGES_QUERY.format(name=Literal(name))


def _show_role(cur, name):
    role, privileges_query = (_redshift_role if _is_redshift(cur) else _pg_role)(cur, name)
    if role is None:
        return [(None, None, None, f"No such user: {name}")]

    results = [(None, role[1], role[0], None)]
    try:
        _, rows = _fetch(cur, privileges_query)
    except psycopg.Error as e:
        _logger.debug("Privilege lookup failed: %s", e)
        results.append((None, None, None, f"Cannot read privileges: {e}"))
        return results

    rows = _group_privileges(rows)
    status = f"{len(rows)} objects with privileges granted directly to {name}" if rows else f"No privileges granted directly to {name}"
    results.append((None, rows, ["type", "schema", "object", "privileges"], status))
    return results


def list_users(cur, pattern, verbose):
    name = pattern.strip().strip("'\"")
    if name:
        return _show_role(cur, name)
    return list_roles(cur, "", verbose)
