from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from pgcli import schema_clipboard


def test_macos_clipboard_receives_exact_text_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(schema_clipboard.sys, 'platform', 'darwin')
    monkeypatch.setattr(schema_clipboard.subprocess, 'run', run)
    text = "SELECT '한글';\n-- literal $(example)"
    schema_clipboard.copy_text(text)
    assert run.call_args.args[0] == ['pbcopy']
    assert run.call_args.kwargs['input'] == text.encode('utf-8')
    assert run.call_args.kwargs['timeout'] == 3
    assert 'shell' not in run.call_args.kwargs


def test_clipboard_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_clipboard.sys, 'platform', 'darwin')
    monkeypatch.setattr(schema_clipboard.subprocess, 'run', Mock(side_effect=subprocess.TimeoutExpired('pbcopy', 3)))
    with pytest.raises(subprocess.TimeoutExpired):
        schema_clipboard.copy_text('SELECT 1')
