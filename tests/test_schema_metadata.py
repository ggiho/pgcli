from unittest.mock import MagicMock
import pytest
from pgcli import schema_metadata


def test_postgres_metadata_preserves_source_and_binds_names(monkeypatch):
    source = MagicMock()
    source.closed = False
    source.info.dsn = 'dbname=fixture'
    source.info.password = 'fixture-only'
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [(123, 'r')]
    cursor.fetchall.side_effect = [
        [('id', 'integer', True, None, True, 'identifier')],
        [('pk', 'PRIMARY KEY (id)')],
        [('pk', 'CREATE UNIQUE INDEX pk ON t(id)')],
    ]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(schema_metadata.psycopg, 'connect', connect)
    provider = schema_metadata.SchemaMetadata(source)
    result = provider.describe('a"b', 'order')
    assert result.columns[0].primary and not result.columns[0].nullable
    assert 'PRIMARY KEY' in result.definition
    assert result.indexes[0].name == 'pk'
    assert cursor.execute.call_args_list[0].args[1] == ('a"b', 'order')
    assert provider.select_sql('a"b', 'order') == 'SELECT * FROM "a""b"."order" LIMIT 100;'
    source.cursor.assert_not_called()
    assert connect.call_args.kwargs['password'] == 'fixture-only'
    assert connect.call_args.kwargs['options'] == '-c statement_timeout=5000'
    connection.__exit__.assert_called_once()


def test_postgres_failed_listing_closes_metadata_connection(monkeypatch):
    source = MagicMock()
    source.closed = False
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError('denied')
    monkeypatch.setattr(schema_metadata.psycopg, 'connect', lambda *a, **kw: connection)
    with pytest.raises(RuntimeError):
        schema_metadata.SchemaMetadata(source).tables()
    connection.__exit__.assert_called_once()
