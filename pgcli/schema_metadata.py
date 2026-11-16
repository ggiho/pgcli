"""Read-only PostgreSQL metadata on separate, bounded connections."""

from __future__ import annotations

import psycopg
from psycopg import sql


class SchemaMetadata:
    def __init__(self, source):
        self.source = source

    def _connect(self):
        if self.source.closed:
            raise RuntimeError('Database connection is closed.')
        return psycopg.connect(
            self.source.info.dsn,
            password=self.source.info.password,
            autocommit=True,
            connect_timeout=5,
            options='-c statement_timeout=5000',
            application_name='pgcli-schema-browser',
        )

    def tables(self):
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT n.nspname,c.relname,CASE WHEN c.relkind IN ('v','m') THEN 'VIEW' ELSE 'TABLE' END "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%' "
                "AND has_schema_privilege(n.oid,'USAGE') AND has_table_privilege(c.oid,'SELECT') "
                "ORDER BY 1,2 LIMIT 2000"
            )
            return list(cur.fetchall())

    def describe(self, schema, name):
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                'SELECT c.oid,c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace '
                'WHERE n.nspname=%s AND c.relname=%s',
                (schema, name),
            )
            relation = cur.fetchone()
            if relation is None:
                raise ValueError('Object no longer exists.')
            oid, kind = relation
            cur.execute(
                'SELECT a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,'
                'pg_catalog.pg_get_expr(d.adbin,d.adrelid) FROM pg_catalog.pg_attribute a '
                'LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum '
                'WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum',
                (oid,),
            )
            lines = [self.qualified_name(schema, name), '', 'Columns']
            for column, datatype, notnull, default in cur.fetchall():
                lines.append(f'{column}  {datatype}  {"NOT NULL" if notnull else "NULL"}')
                if default is not None:
                    lines.append(f'  Default: {default}')
            cur.execute(
                'SELECT conname,pg_catalog.pg_get_constraintdef(oid,true) FROM pg_catalog.pg_constraint WHERE conrelid=%s ORDER BY conname',
                (oid,),
            )
            lines.extend(['', 'Constraints'])
            lines.extend(f'{n}: {definition}' for n, definition in cur.fetchall())
            cur.execute(
                'SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname',
                (schema, name),
            )
            lines.extend(['', 'Indexes'])
            lines.extend(str(definition) for _n, definition in cur.fetchall())
            if kind in ('v', 'm'):
                cur.execute('SELECT pg_catalog.pg_get_viewdef(%s,true)', (oid,))
                lines.extend(['', 'View definition', cur.fetchone()[0]])
            return '\n'.join(lines)

    @staticmethod
    def qualified_name(schema, name):
        return sql.Identifier(schema, name).as_string()

    @classmethod
    def select_sql(cls, schema, name):
        return f'SELECT * FROM {cls.qualified_name(schema, name)} LIMIT 100;'
