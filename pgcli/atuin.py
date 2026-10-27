"""Query history stored in atuin, plus atuin's own interactive picker.

The picker invocation mirrors what ``atuin init zsh`` generates, so the UI, keys
and filter mode match the user's shell.

Behaviour of the atuin CLI that this relies on (verified against atuin 18.24):

* ``atuin search --limit N`` picks the **newest** N entries and prints them
  oldest first. ``--reverse`` does not just flip that output: it flips the
  query, so with ``--limit`` it picks the *oldest* N. The newest-first order
  :meth:`History.load_history_strings` must return is therefore made here, by
  reversing the output.
* ``--include-duplicates`` is required, because prompt_toolkit's
  history-based auto-suggest ranks by how often an entry appears.
* An entry only reaches the record store (and so sync) once ``history end``
  finishes it. prompt_toolkit stores the line on accept, before the query
  runs, so :meth:`AtuinHistory.end` is called by pgcli afterwards with the
  outcome. Like the zsh hook it runs in the background, since it may sync.
* The interactive picker ignores ``--author`` and ``--cwd`` but does honour
  ``--filter-mode session``, so entries are recorded under one deterministic
  synthetic session. Without this the picker would offer shell commands, which
  ``enter_accept`` would then run as SQL. Opened from the up-arrow, it uses
  ``filter_mode_shell_up_key_binding`` instead, so that is forced too.
* atuin draws its TUI on **stdout** and prints the chosen command on
  **stderr** (which is why the zsh widget swaps the two with ``3>&1 1>&2 2>&3``).
* ``ATUIN_QUERY`` seeds the search box with the current buffer.
"""

import hashlib
import logging
import os
import re
from shutil import which
import subprocess
from typing import Iterable, Optional

from prompt_toolkit.history import History
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
import sqlparse

logger = logging.getLogger(__name__)

ACCEPT_PREFIX = "__atuin_accept__:"


def is_available() -> bool:
    return which("atuin") is not None


# CREATE/ALTER ROLE/USER/GROUP with a PASSWORD clause, after any leading comments.
_PASSWORD_STATEMENT = re.compile(r"(create|alter)\s+(role|user|group)\b.*\bpassword\b", re.IGNORECASE | re.DOTALL)


def carries_password(text: str) -> bool:
    """Whether any statement in *text* sets a password, or is ``\\password``.

    Those must not reach atuin, which shows them in the shell's search and syncs
    them. Any PASSWORD clause counts, ``PASSWORD NULL`` included: it holds no
    secret, but is not worth telling apart.
    """
    for statement in sqlparse.split(text):
        statement = sqlparse.format(statement, strip_comments=True).strip()
        if statement.startswith("\\password") or _PASSWORD_STATEMENT.match(statement):
            return True
    return False


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
        # id of the entry store_string started and end has not yet finished.
        self._pending_id = None

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
            # The user's filter_mode would otherwise scope this to one host,
            # directory or session.
            "--filter-mode",
            "global",
            "--include-duplicates",
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
            entries = [record for record in out.split("\0") if record][::-1]
        if self.legacy is not None:
            entries.extend(self.legacy.load_history_strings())
        return entries

    def store_string(self, string: str) -> None:
        # An entry still pending was not run by run_cli: \e stores its own
        # line, then prompts for the edited SQL, which is stored in turn.
        self.end(True)
        if carries_password(string):
            logger.debug("not storing a statement that sets a password in atuin")
            return
        # "--" so SQL starting with "-- comment" is not taken for an option.
        out = self._atuin(
            "history",
            "start",
            "--author",
            self.author,
            "--",
            string,
            env=dict(os.environ, ATUIN_SESSION=session_id(self.author)),
        )
        self._pending_id = out.strip() if out else None

    def end(self, success: bool) -> None:
        """Finish the entry stored last with the outcome of the query it ran.

        Does nothing if nothing is pending, e.g. prompt_toolkit skipped storing a
        repeat of the previous entry, or ``history start`` failed.
        """
        history_id, self._pending_id = self._pending_id, None
        if not history_id:
            return
        try:
            # Not waited for: like the zsh hook's `( ... &)`, since it may sync.
            # Its own session, so a Ctrl-C at the prompt cannot cut that short.
            subprocess.Popen(
                ("atuin", "history", "end", "--exit", "0" if success else "1", "--", history_id),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            logger.debug("atuin history end failed: %s", e)


def search_history(event: KeyPressEvent, author: str) -> bool:
    """Let atuin pick a history entry and put it in the buffer, as its up-arrow widget does.

    Returns False if atuin could not be run or failed, so the caller can fall back.
    """
    buffer = event.current_buffer

    # --author is silently ignored here, so scope via the synthetic session.
    # --shell-up-key-binding tells atuin it was opened from the up-arrow, which
    # it treats differently from ctrl-r (see atuin's shell integration).
    args = ["atuin", "search", "-i", "--filter-mode", "session", "--shell-up-key-binding"]

    env = dict(
        os.environ,
        ATUIN_SHELL="zsh",
        ATUIN_QUERY=buffer.text,
        ATUIN_SESSION=session_id(author),
        # With --shell-up-key-binding this setting beats --filter-mode, so a
        # user's value could otherwise bring shell commands into the picker.
        ATUIN_FILTER_MODE_SHELL_UP_KEY_BINDING="session",
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

    if proc.returncode != 0:
        # stderr is then an error message, not a pick.
        logger.debug("atuin search exited %s: %s", proc.returncode, proc.stderr.strip())
        return False

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
