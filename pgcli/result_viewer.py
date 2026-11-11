"""Per-process bridge to the external SQL result pager."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import click


class ResultViewer:
    """Own one private result cache without touching the query buffer."""

    def __init__(self, command: Path) -> None:
        self.command = command
        self.directory = TemporaryDirectory(prefix='sql-viewer-')
        self.path = self.directory.name
        os.environ['SQL_VIEWER_SESSION'] = self.path
        atexit.register(self.close)

    def close(self) -> None:
        self.directory.cleanup()
        if os.environ.get('SQL_VIEWER_SESSION') == self.path:
            os.environ.pop('SQL_VIEWER_SESSION', None)
        atexit.unregister(self.close)

    def _run(self, option: str, text: str | None = None) -> None:
        try:
            subprocess.run(
                [str(self.command), option],
                input=text,
                stdin=subprocess.DEVNULL if text is None else None,
                text=True,
                env=dict(os.environ, SQL_VIEWER_SESSION=self.path),
                check=False,
            )
        except OSError as error:
            click.echo(f'Could not open result viewer: {error}', err=True)

    def resume(self) -> None:
        """Reopen saved bytes; no SQL client or database connection is involved."""
        self._run('--resume')

    def remember(self, text: str) -> None:
        """Cache output that the client prints inline instead of paging."""
        self._run('--remember', text)


def configured_result_viewer() -> ResultViewer | None:
    command = Path.home() / '.local/libexec/sql-pager/viewer'
    if 'sql-pager/viewer' not in os.environ.get('PAGER', '') or not command.is_file():
        return None
    binary = Path.home() / '.local/bin/sql-viewer'
    try:
        info = subprocess.run([str(binary), '--help'], capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if info.returncode != 0 or '--view-state' not in info.stdout:
        return None
    return ResultViewer(command)
