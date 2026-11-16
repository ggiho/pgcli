from types import SimpleNamespace
from unittest.mock import Mock
from prompt_toolkit.buffer import Buffer
from pgcli import key_bindings, prompt_commands, schema_browser


def test_no_function_keys_are_bound():
    cli = SimpleNamespace(atuin_keys=False)
    kb = key_bindings.pgcli_bindings(cli)
    assert not any(any(str(key).startswith('Keys.F') and str(key)[6:].isdigit() for key in binding.keys) for binding in kb.bindings)
    assert {'smart', 'multiline', 'mode', 'explain'} <= cli.prompt_actions.keys()


def test_browse_returns_draft_without_query(monkeypatch):
    cli = SimpleNamespace(pgexecute=SimpleNamespace(conn=Mock()), syntax_style='native', cli_style={})
    monkeypatch.setattr(schema_browser, 'browse_schema', lambda *a, **kw: ('select', 'SELECT * FROM "public"."t" LIMIT 100;'))
    assert prompt_commands.run_prompt_command(cli, r'\browse') == (True, 'SELECT * FROM "public"."t" LIMIT 100;')
    cli.pgexecute.conn.cursor.assert_not_called()


def test_toggle_dispatch_preserves_draft():
    buffer = Buffer()
    buffer.text = 'SELECT unfinished'
    handler = Mock()
    cli = SimpleNamespace(prompt_actions={'smart': handler}, prompt_app=SimpleNamespace(app=object(), default_buffer=buffer))
    assert prompt_commands.run_prompt_command(cli, r'\smart') == (True, None)
    handler.assert_called_once()
    assert buffer.text == 'SELECT unfinished'


def test_explain_mode_preserves_special_command_dispatch():
    from pgcli.pgexecute import PGExecute

    executor = object.__new__(PGExecute)
    executor.conn = Mock()
    executor.conn.closed = 0
    executor.conn.cursor.return_value.protocol_error = False
    executor.reset_expanded = False
    executor.execute_normal_sql = Mock()
    special = Mock()
    special.execute.return_value = [(None, None, None, 'special handled')]
    result = list(executor.run(r'\q', pgspecial=special, explain_mode=True))
    assert result[0][3] == 'special handled'
    executor.execute_normal_sql.assert_not_called()
