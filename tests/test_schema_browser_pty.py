"""Real terminal checks for inspect/filter/back and explicit draft handoff."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX terminal')
HARNESS = r'''
import json
from unittest.mock import Mock
from pgcli.schema_browser import SchemaBrowser
from pgcli.schema_model import Column,Structure
provider=Mock()
provider.tables.return_value=[('shop','orders','TABLE')]
provider.describe.return_value=Structure(
 [Column('id','bigint',False,True),Column('amount','numeric',True,False,'0','Order amount')],
 [], 'CREATE TABLE orders (id bigint, amount numeric);')
provider.select_sql.return_value='SELECT * FROM "shop"."orders" LIMIT 100;'
ui=SchemaBrowser(provider)
result=ui.app.run(pre_run=ui.start)
print('RESULT_JSON '+json.dumps(result),flush=True)
'''


def test_inspection_and_back_do_not_submit_query() -> None:
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    initial = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    child = subprocess.Popen(
        [sys.executable, '-c', HARNESS],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=dict(os.environ, TERM='xterm-256color', PROMPT_TOOLKIT_NO_CPR='1'),
        start_new_session=True,
    )
    output = bytearray()

    def until(marker: bytes, offset: int = 0, timeout: float = 10) -> None:
        end = time.monotonic() + timeout
        while marker not in output[offset:]:
            assert time.monotonic() < end, bytes(output)[-3000:]
            if select.select([master], [], [], 0.02)[0]:
                output.extend(os.read(master, 65536))

    try:
        until(b'Find table', timeout=30)
        until(b'amount')
        os.write(master, b'orders\ramount\r')
        until(b'Order amount')
        assert child.poll() is None and b'RESULT_JSON' not in output
        offset = len(output)
        os.write(master, b'\x1b')
        until(b'Enter inspect', offset, 0.5)
        os.write(master, b'\x7f\x7f\x7f\x7f\x7f\x7forders\r')
        # Enter returns to the existing filtered column inspector.
        os.write(master, b'\r\t\t\r')
        until(b'RESULT_JSON')
        child.wait(timeout=5)
        while select.select([master], [], [], 0.05)[0]:
            output.extend(os.read(master, 65536))
        assert json.loads(bytes(output).split(b'RESULT_JSON ', 1)[1].splitlines()[0]) == [
            'select',
            'SELECT * FROM "shop"."orders" LIMIT 100;',
        ]
        assert b'\x1b[?1049h' in output and b'\x1b[?1049l' in output
        restored = termios.tcgetattr(slave)
        restored[3] &= ~getattr(termios, 'PENDIN', 0)
        initial[3] &= ~getattr(termios, 'PENDIN', 0)
        assert restored == initial
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        os.close(master)
        os.close(slave)
