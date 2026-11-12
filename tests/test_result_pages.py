from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from pgcli.result_pages import PagedRows, simple_relation
from pgcli.result_viewer import ResultViewer


@pytest.mark.parametrize(
    'sql,relation',
    [
        ('SELECT * FROM orders', (None, 'orders')),
        ('select id, amount from public.orders where id > 2 order by id limit 23', ('public', 'orders')),
        ("SELECT * FROM orders WHERE label IN ('SELECT', 'INTO')", (None, 'orders')),
        ('SELECT id FROM orders; SELECT id FROM orders', None),
        ('WITH t AS (SELECT * FROM orders) SELECT * FROM t', None),
        ('SELECT count(*) FROM orders', None),
        ('SELECT nextval(\'s\') FROM orders', None),
        ('SELECT * FROM orders FOR UPDATE', None),
        ('SELECT * FROM orders LOCK IN SHARE MODE', None),
        ('SELECT * INTO snapshot FROM orders', None),
        ('SELECT * FROM orders JOIN lines ON orders.id=lines.id', None),
        ('SELECT * FROM orders WHERE id IN (SELECT id FROM lines)', None),
        ('SELECT @counter:=id FROM orders', None),
        ('SELECT id::text FROM orders', None),
        ('SELECT 1', None),
    ],
)
def test_conservative_paging_eligibility(sql, relation):
    assert simple_relation(sql) == relation


class Rows:
    def __init__(self, count=1001, fail_at=None):
        self.count = count
        self.position = 0
        self.calls = 0
        self.fail_at = fail_at

    def __iter__(self):
        return self

    def __next__(self):
        self.calls += 1
        if self.position == self.fail_at:
            raise RuntimeError('read failed')
        if self.position >= self.count:
            raise StopIteration
        i = self.position
        self.position += 1
        return (i, None if i % 5 == 0 else i, 'NULL' if i % 3 == 0 else ' padded ')


def source(rows, closed, cancelled, live=False):
    return PagedRows(iter(rows), [('id',), ('amount',), ('label',)], lambda: closed.append(True), lambda: cancelled.append(True), live=live)


def test_batches_fetch_lookahead_only_and_keep_real_nulls(tmp_path):
    rows = Rows()
    closed = []
    cancelled = []
    viewer = ResultViewer(tmp_path / 'unused')
    try:
        viewer.begin(source(rows, closed, cancelled))
        assert len(viewer.rows) == 500 and rows.calls == 501 and viewer.has_more
        assert viewer.nulls[0] == [False, True, False]
        assert viewer.rows[0][2] == 'NULL' and viewer.rows[1][2] == ' padded '
        assert viewer._text('a\n\x1b\x9b') == r'a\n\x1b\x9b'
        marker = json.loads((Path(viewer.path) / 'query.json').read_text())
        assert marker['loaded'] == 500 and 'rows' not in marker
        batch = viewer._pull()
        assert batch['base'] == 500 and len(batch['rows']) == 500
        assert rows.calls == 1001
        batch = viewer._pull()
        assert batch['base'] == 1000 and len(batch['rows']) == 1
        assert not viewer.has_more and viewer.source is None
        assert closed == [True] and not cancelled
    finally:
        viewer.close()


def test_closed_cursor_keeps_loaded_preview_without_reexecution(tmp_path):
    rows = Rows()
    closed = []
    cancelled = []
    viewer = ResultViewer(tmp_path / 'unused')
    try:
        viewer.begin(source(rows, closed, cancelled, live=True))
        viewer.release_result()
        assert cancelled == [True] and closed == [True]
        assert rows.calls == 501 and len(viewer.rows) == 500
        assert not viewer._document()['can_fetch'] and viewer.has_more
        assert 'Cursor closed' in viewer.message
    finally:
        viewer.close()


def test_fetch_error_keeps_prefix_and_does_not_claim_eof(tmp_path):
    viewer = ResultViewer(tmp_path / 'unused')
    closed = []
    cancelled = []
    try:
        viewer.begin(source(Rows(fail_at=750), closed, cancelled, live=True))
        viewer._pull()
        assert viewer.fetch_error and len(viewer.rows) == 750
        assert viewer.has_more and not viewer._document()['can_fetch']
        assert 'read failed' in viewer.message
    finally:
        viewer.close()


def test_initial_fetch_error_is_reported_as_failure(tmp_path):
    viewer = ResultViewer(tmp_path / 'unused')
    try:
        with pytest.raises(RuntimeError, match='read failed'):
            viewer.begin(source(Rows(fail_at=0), [], []))
    finally:
        viewer.close()


def test_cache_cap_stops_cursor_and_marks_unfetched_tail(tmp_path):
    viewer = ResultViewer(tmp_path / 'unused')
    closed = []
    cancelled = []
    try:
        viewer.begin(source(Rows(), closed, cancelled, live=True))
        viewer.cache_bytes = 64 * 1024 * 1024
        viewer._pull()
        assert len(viewer.rows) == 500 and viewer.has_more and viewer.source is None
        assert 'limit reached' in viewer.message
        assert closed == [True] and cancelled == [True]
    finally:
        viewer.close()


@pytest.mark.skipif(os.name != 'posix', reason='private Unix result socket')
def test_socket_service_reopens_same_cursor_and_cache(tmp_path, monkeypatch):
    from pgcli import result_viewer as module

    binary = tmp_path / '.local/bin/sql-viewer'
    binary.parent.mkdir(parents=True)
    binary.write_text('''#!/usr/bin/env python3
import socket,sys,json,os
path=sys.argv[sys.argv.index('--query-socket')+1]
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
 s.connect(path)
 with s.makefile('rwb') as f:
  result=[]
  for op in ['init','next']:
   f.write((json.dumps({'op':op})+'\\n').encode());f.flush()
   batch=json.loads(f.readline());result.append([batch['base'],len(batch['rows'])])
  with open(os.environ['RESULT_TEST_LOG'],'a') as log:log.write(json.dumps(result)+'\\n')
''')
    binary.chmod(0o755)
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    log = tmp_path / 'rpc.log'
    monkeypatch.setenv('RESULT_TEST_LOG', str(log))
    viewer = ResultViewer(tmp_path / 'unused')
    rows = Rows()
    try:
        viewer.begin(source(rows, [], []))
        viewer.show(force=True)
        viewer.resume()
        assert [json.loads(line) for line in log.read_text().splitlines()] == [[[0, 500], [500, 500]], [[0, 1000], [1000, 1]]]
        assert rows.position == 1001 and not viewer.has_more
    finally:
        viewer.close()


@pytest.mark.skipif(os.name != 'posix', reason='private Unix result socket')
def test_older_viewer_keeps_toggle_without_enabling_paging(tmp_path, monkeypatch):
    from pgcli import result_viewer as module

    binary = tmp_path / '.local/bin/sql-viewer'
    binary.parent.mkdir(parents=True)
    command = tmp_path / '.local/libexec/sql-pager/viewer'
    command.parent.mkdir(parents=True)
    command.touch()
    binary.write_text('#!/bin/sh\necho "Integration: --view-state PATH"\n')
    binary.chmod(0o755)
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('PAGER', str(command))
    viewer = module.configured_result_viewer()
    assert viewer is not None
    try:
        assert not viewer.paging_enabled
    finally:
        viewer.close()


@pytest.mark.skipif(os.name != 'posix', reason='private Unix result socket')
def test_closing_viewer_during_fetch_cancels_without_replaying_sql(tmp_path, monkeypatch):
    from pgcli import result_viewer as module

    cancelled = threading.Event()

    class SlowRows(Rows):
        def __next__(self):
            if self.position == 501:
                if not cancelled.wait(5):
                    raise RuntimeError('cancel did not arrive')
                raise RuntimeError('cancelled read')
            return super().__next__()

    binary = tmp_path / '.local/bin/sql-viewer'
    binary.parent.mkdir(parents=True)
    binary.write_text('''#!/usr/bin/env python3
import socket,sys,json,time
path=sys.argv[sys.argv.index('--query-socket')+1]
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
 s.connect(path)
 with s.makefile('rwb') as f:
  f.write(b'{"op":"init"}\\n');f.flush();json.loads(f.readline())
  f.write(b'{"op":"next"}\\n');f.flush();time.sleep(.15)
''')
    binary.chmod(0o755)
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    rows = SlowRows()
    source = PagedRows(iter(rows), [('id',), ('amount',), ('label',)], lambda: None, cancelled.set, live=True)
    viewer = ResultViewer(tmp_path / 'unused')
    try:
        viewer.begin(source)
        viewer.show(force=True)
        assert cancelled.is_set() and source.closed
        assert viewer.source is None and not viewer._document()['can_fetch']
        assert not viewer.fetch_error
        assert rows.position == 501
    finally:
        viewer.close()


@pytest.mark.parametrize(
    'autocommit,status,metadata',
    [
        (False, 0, ('r', 'p', False, False)),
        (True, 2, ('r', 'p', False, False)),
        (True, 0, ('v', 'p', False, False)),
        (True, 0, ('r', 't', False, False)),
        (True, 0, ('r', 'p', True, False)),
        (True, 0, ('r', 'p', False, True)),
    ],
)
def test_postgres_keeps_transactions_and_special_relations_buffered(autocommit, status, metadata):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from pgcli.result_pages import postgres_stream

    connection = MagicMock()
    connection.autocommit = autocommit
    connection.info.transaction_status = status
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = metadata
    assert postgres_stream(SimpleNamespace(conn=connection), 'SELECT * FROM orders') is None
    connection.cursor.return_value.stream.assert_not_called()


@pytest.mark.parametrize('table,reference', [('ORDERS AS o', 'ORDERS'), ('"MySchema"."Orders" o', '"MySchema"."Orders"')])
def test_postgres_metadata_preserves_identifier_case_and_quoting(table, reference):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from pgcli.result_pages import postgres_stream

    connection = MagicMock()
    connection.autocommit = True
    connection.info.transaction_status = 0
    meta = connection.cursor.return_value.__enter__.return_value
    meta.fetchone.return_value = ('v', 'p', False, False)
    assert postgres_stream(SimpleNamespace(conn=connection), 'SELECT * FROM ' + table) is None
    assert meta.execute.call_args.args[1] == (reference,)


@pytest.mark.parametrize('value,always', [('가' * 50, False), ('short', True)])
def test_inline_fit_uses_terminal_cells_and_respects_always(tmp_path, monkeypatch, value, always):
    from pgcli import result_viewer as module
    from types import SimpleNamespace

    launched = []
    monkeypatch.setattr(module.shutil, 'get_terminal_size', lambda: os.terminal_size((80, 24)))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *args: launched.append(args) or SimpleNamespace(poll=lambda: 0, wait=lambda: 0))
    if always:
        monkeypatch.setenv('SQL_VIEWER_ALWAYS', '1')
    else:
        monkeypatch.delenv('SQL_VIEWER_ALWAYS', raising=False)
    viewer = ResultViewer(tmp_path / 'unused')
    try:
        viewer.begin(PagedRows(iter([(value,)]), [('v',)], lambda: None, lambda: None, live=False))
        viewer.show()
        assert len(launched) == 1
    finally:
        viewer.close()
