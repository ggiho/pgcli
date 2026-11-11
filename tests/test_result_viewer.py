import os
from pathlib import Path

import pytest

from pgcli.result_viewer import ResultViewer, configured_result_viewer


@pytest.mark.skipif(os.name != 'posix', reason='SQL viewer requires a POSIX terminal')
def test_result_sessions_are_private_isolated_and_removed(tmp_path, monkeypatch):
    monkeypatch.delenv('SQL_VIEWER_SESSION', raising=False)
    command = tmp_path / 'viewer'
    command.write_text('''#!/bin/sh
case "$1" in
  --remember) cat > "$SQL_VIEWER_SESSION/result" ;;
  --resume) cp "$SQL_VIEWER_SESSION/result" "$SQL_VIEWER_SESSION/resumed" ;;
esac
''')
    command.chmod(0o755)
    first, second = ResultViewer(command), ResultViewer(command)
    try:
        assert first.path != second.path
        assert Path(first.path).stat().st_mode & 0o077 == 0
        first.remember('first connection')
        second.remember('second connection')
        first.resume()
        assert (Path(first.path) / 'resumed').read_text() == 'first connection'
        assert (Path(second.path) / 'result').read_text() == 'second connection'
        first.close()
        assert not Path(first.path).exists()
        assert __import__('os').environ['SQL_VIEWER_SESSION'] == second.path
    finally:
        first.close()
        second.close()
    assert not Path(second.path).exists()


def test_other_pagers_do_not_create_a_viewer_session(monkeypatch):
    monkeypatch.setenv('PAGER', 'less -SR')
    assert configured_result_viewer() is None


def test_result_toggle_does_not_reserve_a_user_shortcut(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pgcli import key_bindings

    monkeypatch.setattr(key_bindings, 'configured_result_viewer', lambda: Mock())
    kb = key_bindings.pgcli_bindings(SimpleNamespace(atuin_keys=False))
    assert not any(item.keys in [('escape', 't'), ('c-g',), ('c-x', 'c-v')] for item in kb.bindings)
