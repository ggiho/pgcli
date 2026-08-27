"""Query history stored in atuin, plus atuin's own interactive picker.

The picker invocation mirrors what ``atuin init zsh`` generates, so the UI, keys
and filter mode match the user's shell.

Behaviour of the atuin CLI that this relies on (verified against atuin 18.19):

* ``atuin search`` prints **oldest first**; ``--reverse`` is what yields the
  newest-first order :meth:`History.load_history_strings` must return. Its
  ``--help`` describes the opposite.
* ``--include-duplicates`` is required, because prompt_toolkit's
  history-based auto-suggest ranks by how often an entry appears.
* ``history end`` is not called: it demands an exit code we do not have, since
  prompt_toolkit stores the line on accept, before the query runs. atuin leaves
  such entries at ``exit=-1``, which is the honest value.
* The interactive picker ignores ``--author`` and ``--cwd`` but does honour
  ``--filter-mode session``, so entries are recorded under one deterministic
  synthetic session. Without this the picker would offer shell commands, which
  ``enter_accept`` would then run as SQL.
* atuin draws its TUI on **stdout** and prints the chosen command on
  **stderr** (which is why the zsh widget swaps the two with ``3>&1 1>&2 2>&3``).
* ``ATUIN_QUERY`` seeds the search box with the current buffer.
"""

import hashlib
import logging
import os
from shutil import which
import subprocess
from typing import Iterable, Optional

from prompt_toolkit.history import History
from prompt_toolkit.key_binding.key_processor import KeyPressEvent

logger = logging.getLogger(__name__)

ACCEPT_PREFIX = "__atuin_accept__:"


def is_available() -> bool:
    return which("atuin") is not None


def session_id(author: str) -> str:
    """Stable synthetic atuin session id for pgcli's entries.

    Derived from the author so a different tag gets a different session, which
    is what keeps pgcli's picker from offering mycli's SQL.
    """
    return hashlib.sha256(f"pgcli-history:{author}".encode()).hexdigest()[:32]


class AtuinHistory(History):
    """History backed by atuin rather than a flat file.

    Entries are tagged with an author (``pgcli`` by default) so they stay
    distinguishable from shell history::

        atuin search --author pgcli

    atuin only holds what this class wrote, so an existing file history can be
    passed as ``legacy``; its entries are appended after the atuin ones. That
    keeps pre-atuin queries reachable without bulk-importing them.
    """

    #: atuin is a local sqlite read; this only guards against a wedged process.
    TIMEOUT = 5.0

    def __init__(self, author: str = "pgcli", limit: int = 5000, legacy: Optional[History] = None) -> None:
        super().__init__()
        self.author = author
        self.limit = limit
        self.legacy = legacy

    def _atuin(self, *args: str, env: Optional[dict] = None) -> Optional[str]:
        """Run atuin and return stdout, or None if it could not be run."""
        try:
            proc = subprocess.run(("atuin", *args), capture_output=True, text=True, timeout=self.TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("atuin %s failed: %s", args[:1], e)
            return None
        if proc.returncode != 0:
            logger.debug("atuin %s exited %s: %s", args[:1], proc.returncode, proc.stderr.strip())
            return None
        return proc.stdout

    def load_history_strings(self) -> Iterable[str]:
        out = self._atuin(
            "search",
            "--author",
            self.author,
            "--include-duplicates",
            "--reverse",
            "--print0",
            "--cmd-only",
            "--limit",
            str(self.limit),
        )
        if out is None:
            logger.debug("atuin unreadable; using legacy history only")
            entries = []
        else:
            # --print0 terminates every record, so the trailing split is empty.
            entries = [record for record in out.split("\0") if record]
        if self.legacy is not None:
            entries.extend(self.legacy.load_history_strings())
        return entries

    def store_string(self, string: str) -> None:
        self._atuin(
            "history",
            "start",
            "--author",
            self.author,
            string,
            env=dict(os.environ, ATUIN_SESSION=session_id(self.author)),
        )


def search_history(event: KeyPressEvent, author: str, up_key_binding: bool = False) -> bool:
    """Let atuin pick a history entry and put it in the buffer.

    Returns False if atuin could not be run, so the caller can fall back.
    """
    buffer = event.current_buffer

    # --author is silently ignored here, so scope via the synthetic session.
    args = ["atuin", "search", "-i", "--filter-mode", "session"]
    if up_key_binding:
        # Tells atuin it was opened from the up-arrow, which it treats
        # differently from ctrl-r (see atuin's shell integration).
        args.append("--shell-up-key-binding")

    env = dict(
        os.environ,
        ATUIN_SHELL="zsh",
        ATUIN_QUERY=buffer.text,
        ATUIN_SESSION=session_id(author),
    )

    try:
        # No timeout: the user drives this UI. stdout stays on the terminal so
        # the TUI draws; the pick comes back on stderr.
        proc = subprocess.run(args, stdout=None, stderr=subprocess.PIPE, text=True, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("atuin search could not run: %s", e)
        return False

    # Redraw: atuin left the alternate screen and prompt_toolkit does not know.
    event.app.renderer.reset()
    event.app.invalidate()

    pick = proc.stderr.strip()
    if not pick:
        # Escape / no match. The key was still handled.
        return True

    accept = pick.startswith(ACCEPT_PREFIX)
    if accept:
        pick = pick[len(ACCEPT_PREFIX) :]

    buffer.text = pick
    buffer.cursor_position = len(pick)
    if accept:
        buffer.validate_and_handle()
    return True
