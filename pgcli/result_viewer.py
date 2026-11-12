"""Per-process bridge to the external SQL result pager."""

from __future__ import annotations

import atexit
import json
import shutil
import socket
import threading
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

import click
from prompt_toolkit.utils import get_cwidth


_DISPLAY_ESCAPES = {i: '\\x' + format(i, '02x') for i in [*range(32), *range(127, 160)]}
_DISPLAY_ESCAPES.update({10: r'\n', 13: r'\r', 9: r'\t', 127: r'\x7f'})


class ResultViewer:
    """Own one private result cache without touching the query buffer."""

    def __init__(self, command: Path) -> None:
        self.command = command
        self.directory = TemporaryDirectory(prefix='sql-viewer-')
        self.path = self.directory.name
        self.source: Any = None
        self.headers: list[str] = []
        self.raw_rows: list[tuple] = []
        self.rows: list[list[str]] = []
        self.nulls: list[list[bool]] = []
        self.has_more = False
        self.message = ''
        self.cache_bytes = 0
        self.pending: tuple | None = None
        self.null_string = 'NULL'
        self.paging_enabled = os.environ.get('SQL_VIEWER_PAGING', '1') != '0'
        self.fetch_mode = 'cached'
        self.fetch_error = False
        os.environ['SQL_VIEWER_SESSION'] = self.path
        atexit.register(self.close)

    def close(self) -> None:
        try:
            self.release_result()
        except Exception:
            pass
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
        """Restore the loaded preview and its cursor without re-executing SQL."""
        if (Path(self.path) / 'query.json').exists():
            self.show(force=True)
        else:
            self._run('--resume')

    def remember(self, text: str) -> None:
        """Cache output that the client prints inline instead of paging."""
        self._run('--remember', text)

    @property
    def live_cursor(self) -> bool:
        return self.source is not None and self.source.live and not self.source.closed and not self.source.exhausted

    def release_result(self) -> None:
        """Release the previous read before any other use of its connection."""
        if self.source is not None:
            self.source.close()
            self.source = None
            if self.has_more:
                self.message = 'Cursor closed for a new command; loaded rows are cached.'
            self._snapshot()

    def begin(self, source: Any, *, null_string: str = 'NULL') -> None:
        self.release_result()
        self.source = source
        self.headers = source.headers
        self.raw_rows = []
        self.rows = []
        self.nulls = []
        self.has_more = True
        self.message = ''
        self.cache_bytes = 0
        self.fetch_error = False
        self.pending = None
        self.null_string = null_string
        self.fetch_mode = 'streamed' if source.live else 'buffered'
        source.preview = self.raw_rows
        for name in ['view.json', 'result', 'query.json']:
            (Path(self.path) / name).unlink(missing_ok=True)
        self._pull()
        if self.fetch_error and not self.rows:
            raise RuntimeError(self.message)

    def _document(self, start: int = 0) -> dict:
        return {
            'protocol': 1,
            'headers': self.headers,
            'rows': self.rows[start:],
            'nulls': self.nulls[start:],
            'base': start,
            'has_more': self.has_more,
            'can_fetch': self.source is not None and not self.source.closed and self.has_more,
            'message': self.message,
            'fetch_mode': self.fetch_mode,
        }

    def _snapshot(self) -> None:
        target = Path(self.path) / 'query.json'
        staged = target.with_suffix('.new')
        # Rows stay in the owning CLI process; this is only a cache-generation marker.
        staged.write_text(
            json.dumps(
                {'protocol': 1, 'loaded': len(self.rows), 'headers': self.headers, 'has_more': self.has_more, 'message': self.message},
                ensure_ascii=False,
            )
        )
        staged.replace(target)

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            text = '0x' + value.hex()
        else:
            text = str(value)
        return text.translate(_DISPLAY_ESCAPES)

    def _pull(self) -> dict:
        start = len(self.rows)
        batch_bytes = 0
        if self.source is None:
            return self._document(start)
        try:
            while len(self.rows) - start < 500 and batch_bytes < 4 * 1024 * 1024:
                if self.pending is not None:
                    row = self.pending
                    self.pending = None
                else:
                    try:
                        row = self.source.next_row()
                    except StopIteration:
                        self.has_more = False
                        break
                shown = [self.null_string if value is None else self._text(value) for value in row]
                size = sum(len(value.encode('utf-8')) + 32 for value in shown) + 64
                if self.cache_bytes + size > 64 * 1024 * 1024 or len(self.rows) >= 100_000:
                    self.message = 'Preview cache limit reached (64 MiB / 100,000 rows); more rows were not fetched.'
                    self.source.close()
                    self.source = None
                    self.has_more = True
                    break
                self.raw_rows.append(tuple(row))
                self.rows.append(shown)
                self.nulls.append([value is None for value in row])
                self.cache_bytes += size
                batch_bytes += size
            if self.source is not None and self.has_more and self.pending is None:
                try:
                    self.pending = self.source.next_row()
                except StopIteration:
                    self.has_more = False
            if self.source is not None and not self.has_more:
                self.source.close()
                self.source = None
        except Exception as error:
            cancelled = self.source is not None and self.source.cancelled
            self.fetch_error = not cancelled
            self.message = 'Fetching cancelled; loaded rows are cached.' if cancelled else f'Fetching stopped: {error}'
            if self.source is not None:
                try:
                    self.source.close()
                except Exception:
                    pass
                self.source = None
            self.has_more = True
        self._snapshot()
        return self._document(start)

    def show(self, *, force: bool = False) -> None:
        """Service batch requests synchronously; the cursor has one owner."""
        if not self.headers and not self.rows:
            click.echo(self.message or '0 rows')
            return
        force = force or os.environ.get('SQL_VIEWER_ALWAYS') == '1'
        size = shutil.get_terminal_size()
        if not force and not self.has_more and len(self.rows) + 4 < size.lines - 2:
            from cli_helpers.tabular_output import TabularOutputFormatter

            rendered = '\n'.join(
                TabularOutputFormatter().format_output(
                    self.raw_rows,
                    self.headers,
                    format_name='ascii_escaped',
                    preserve_whitespace=True,
                    disable_numparse=True,
                    missing_value=self.null_string,
                )
            )
            if len(rendered.splitlines()) < size.lines - 2 and all(get_cwidth(line) <= size.columns for line in rendered.splitlines()):
                click.echo(rendered)
                return
        address = Path(self.path) / 'query.sock'
        address.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(address))
            listener.listen(1)
            child = subprocess.Popen([
                str(Path.home() / '.local/bin/sql-viewer'),
                '--query-socket',
                str(address),
                '--view-state',
                str(Path(self.path) / 'view.json'),
            ])
            fetching = threading.Event()

            def child_exited() -> None:
                child.wait()
                if fetching.is_set() and self.source is not None:
                    try:
                        self.source.cancel()
                    except Exception:
                        pass

            watcher = threading.Thread(target=child_exited, daemon=True)
            watcher.start()
            listener.settimeout(0.1)
            connection = None
            try:
                while child.poll() is None:
                    try:
                        connection, _ = listener.accept()
                        break
                    except socket.timeout:
                        continue
                if connection is not None:
                    with connection, connection.makefile('rwb') as channel:
                        while child.poll() is None:
                            line = channel.readline()
                            if not line:
                                break
                            request = json.loads(line)
                            if request.get('op') == 'init':
                                response = self._document()
                            elif request.get('op') == 'next':
                                fetching.set()
                                if child.poll() is not None:
                                    fetching.clear()
                                    break
                                try:
                                    response = self._pull()
                                finally:
                                    fetching.clear()
                            else:
                                response = {'protocol': 1, 'error': 'Unknown result request'}
                            channel.write((json.dumps(response, ensure_ascii=False) + '\n').encode())
                            channel.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                child.wait()
                watcher.join(timeout=6)
                if self.source is not None and self.source.cancelled:
                    self.release_result()
                address.unlink(missing_ok=True)


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
    viewer = ResultViewer(command)
    viewer.paging_enabled = viewer.paging_enabled and '--query-socket' in info.stdout
    return viewer
