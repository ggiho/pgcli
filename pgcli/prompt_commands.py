"""Interactive access to former function-key actions."""

from types import SimpleNamespace

import click

HELP = r'''\browse     Search tables and inspect columns/indexes
\smart      Toggle smart completion
\multiline  Toggle multiline input
\mode       Toggle Vi/Emacs editing
\explain    Toggle EXPLAIN mode for subsequent statements
\keys       Show this list (no function keys required)
\?          CLI help'''


def run_prompt_command(cli, text):
    parts = text.strip().rstrip(';').split(maxsplit=1)
    if not parts or parts[0] not in (r'\browse', r'\smart', r'\multiline', r'\mode', r'\explain', r'\keys'):
        return False, None
    if len(parts) != 1:
        click.echo(f'Usage: {parts[0]}')
        return True, None
    command = parts[0][1:]
    if command == 'keys':
        click.echo(HELP)
    elif command == 'browse':
        from .schema_browser import browse_schema
        from .schema_metadata import SchemaMetadata
        from .pgstyle import style_factory

        result = browse_schema(SchemaMetadata(cli.pgexecute.conn), style=style_factory(cli.syntax_style, cli.cli_style))
        if result:
            return True, result[1]
    else:
        cli.prompt_actions[command](SimpleNamespace(app=cli.prompt_app.app, current_buffer=cli.prompt_app.default_buffer))
        click.echo(
            f'EXPLAIN ANALYZE (executes SQL): {"ON" if cli.explain_mode else "OFF"}' if command == 'explain' else f'{command}: updated'
        )
    return True, None
