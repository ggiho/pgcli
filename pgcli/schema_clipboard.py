"""Copy schema text using the desktop clipboard without extra dependencies."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def copy_text(text: str) -> None:
    if sys.platform == 'darwin':
        command = ['pbcopy']
    elif sys.platform == 'win32':
        command = ['clip']
    elif os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
        command = ['wl-copy']
    elif shutil.which('xclip'):
        command = ['xclip', '-selection', 'clipboard']
    elif shutil.which('xsel'):
        command = ['xsel', '--clipboard', '--input']
    else:
        raise OSError('No clipboard tool found (wl-copy, xclip or xsel).')
    data = text.encode('utf-16le' if sys.platform == 'win32' else 'utf-8')
    subprocess.run(command, input=data, check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
