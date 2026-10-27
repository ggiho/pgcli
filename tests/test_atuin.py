import subprocess

import pytest

from pgcli.atuin import ACCEPT_PREFIX, AtuinHistory, search_history, session_id


class FakeAtuin:
    """Stands in for the atuin executable, recording argv and replaying stdout.

    Stands in for both subprocess.run and subprocess.Popen; `history end` is the
    only Popen, and its result is not used.
    """

    def __init__(self, stdout="", stderr="", returncode=0, raises=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls = []
        self.env = None
        self.kwargs = {}

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.env = kwargs.get("env")
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        # A callable stdout answers per call, e.g. a new id for each `history start`.
        stdout = self.stdout(argv) if callable(self.stdout) else self.stdout
        return subprocess.CompletedProcess(argv, self.returncode, stdout=stdout, stderr=self.stderr)


@pytest.fixture
def fake_atuin(monkeypatch):
    def install(**kwargs):
        fake = FakeAtuin(**kwargs)
        monkeypatch.setattr(subprocess, "run", fake)
        monkeypatch.setattr(subprocess, "Popen", fake)
        return fake

    return install


class FakeBuffer:
    def __init__(self, text="", complete_state=None):
        self.text = text
        self.cursor_position = len(text)
        self.complete_state = complete_state
        self.accepted = False

    def validate_and_handle(self):
        self.accepted = True

    def auto_up(self, count=1):
        pass


class FakeEvent:
    def __init__(self, buffer):
        self.current_buffer = buffer
        self.app = type(
            "App",
            (),
            {"renderer": type("R", (), {"reset": lambda self: None})(), "invalidate": lambda self: None},
        )()
        self.arg = 1


def test_session_id_is_stable_and_author_specific():
    assert session_id("pgcli") == session_id("pgcli")
    assert session_id("pgcli") != session_id("other")
    # atuin session ids are 32-char hex.
    assert len(session_id("pgcli")) == 32
    assert all(c in "0123456789abcdef" for c in session_id("pgcli"))


def test_session_id_differs_from_myclis_scheme():
    """pgcli and mycli must not share a session, or each picker shows the other."""
    import hashlib

    mycli = hashlib.sha256(b"mycli-history:mycli").hexdigest()[:32]
    assert session_id("pgcli") != mycli


def test_load_filters_by_author_newest_first_and_keeps_duplicates(fake_atuin):
    fake = fake_atuin(stdout="SELECT 1\0SELECT 1\0SELECT 2\0")
    assert list(AtuinHistory().load_history_strings()) == ["SELECT 2", "SELECT 1", "SELECT 1"]
    argv = fake.calls[0]
    assert argv[:2] == ["atuin", "search"]
    assert argv[argv.index("--author") + 1] == "pgcli"
    # Not the user's filter_mode, which could scope it to one host or directory.
    assert argv[argv.index("--filter-mode") + 1] == "global"
    # Duplicates drive frequency-based auto-suggest ranking.
    assert "--include-duplicates" in argv
    # --reverse would make --limit pick the oldest entries; the order is flipped here instead.
    assert "--reverse" not in argv
    assert argv[argv.index("--limit") + 1] == "5000"
    assert "--cmd-only" in argv


def test_load_preserves_multiline_entries(fake_atuin):
    fake_atuin(stdout="SELECT 1\0SELECT a,\n       b\n  FROM t\0")
    assert list(AtuinHistory().load_history_strings()) == ["SELECT a,\n       b\n  FROM t", "SELECT 1"]


def test_store_records_with_author_and_no_fake_exit(fake_atuin):
    fake = fake_atuin(stdout="01a040f0cb257242b718f2ab411b84ce\n")
    AtuinHistory(author="pgcli-test").store_string("SELECT 1")
    assert fake.calls == [["atuin", "history", "start", "--author", "pgcli-test", "--", "SELECT 1"]]
    # `history end` needs an exit code we cannot know at store time.
    assert not any("end" in call for call in fake.calls)
    # Recording and the picker must agree on the session.
    assert fake.env["ATUIN_SESSION"] == session_id("pgcli-test")


def test_store_keeps_sql_that_starts_with_a_dash(fake_atuin):
    fake = fake_atuin(stdout="01a040f0cb257242b718f2ab411b84ce\n")
    AtuinHistory().store_string("-- note\nSELECT 1")
    # Without "--" atuin rejects this as an unknown option.
    assert fake.calls[0][-2:] == ["--", "-- note\nSELECT 1"]


def numbered_ids():
    """A stdout for FakeAtuin that hands out id1, id2, ... per `history start`."""
    count = 0

    def stdout(argv):
        nonlocal count
        if argv[1:3] != ["history", "start"]:
            return ""
        count += 1
        return f"id{count}\n"

    return stdout


def ends(fake):
    """(exit code, id) of every `history end`, in order."""
    return [(call[call.index("--exit") + 1], call[-1]) for call in fake.calls if call[1:3] == ["history", "end"]]


def starts(fake):
    return [call[-1] for call in fake.calls if call[1:3] == ["history", "start"]]


@pytest.mark.parametrize(
    "text",
    [
        "ALTER ROLE bob PASSWORD 'hunter2'",
        "alter user bob with encrypted password 'hunter2'",
        "CREATE USER bob\n    WITH LOGIN\n    PASSWORD 'hunter2'",
        "Create Role bob Login Password 'hunter2'",
        "CREATE GROUP g PASSWORD 'hunter2'",
        "SELECT 1; ALTER USER bob PASSWORD 'hunter2'; SELECT 2",
        "-- rotate\nALTER USER bob PASSWORD 'hunter2'",
        "/* rotate */ ALTER USER bob PASSWORD 'hunter2'",
        "CREATE USER MAPPING FOR bob SERVER s OPTIONS (user 'bob', password 'hunter2')",
        "\\password",
        "\\password bob",
        # Holds no secret, but is not told apart from the other PASSWORD clauses.
        "ALTER ROLE bob PASSWORD NULL",
    ],
)
def test_store_skips_statements_that_set_a_password(fake_atuin, text):
    fake = fake_atuin(stdout="01a040f0cb257242b718f2ab411b84ce\n")
    history = AtuinHistory()
    history.store_string(text)
    history.end(True)
    assert fake.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "SELECT password FROM users",
        "SELECT user_password FROM accounts WHERE role = 'admin'",
        "ALTER ROLE bob VALID UNTIL 'infinity'",
        "CREATE TABLE password_resets (token text)",
        "ALTER TABLE users ALTER COLUMN password SET NOT NULL",
    ],
)
def test_store_keeps_statements_that_only_mention_password(fake_atuin, text):
    fake = fake_atuin(stdout="01a040f0cb257242b718f2ab411b84ce\n")
    AtuinHistory().store_string(text)
    assert starts(fake) == [text]


def test_skipping_a_password_statement_still_finishes_the_entry_before_it(fake_atuin):
    fake = fake_atuin(stdout=numbered_ids())
    history = AtuinHistory()
    history.store_string("SELECT 1")
    history.store_string("ALTER USER bob PASSWORD 'hunter2'")
    history.end(False)
    assert starts(fake) == ["SELECT 1"]
    assert ends(fake) == [("0", "id1")]


def test_store_finishes_an_entry_still_pending(fake_atuin):
    """\\e stores its own line, then the edited SQL; the first must not be left unfinished."""
    fake = fake_atuin(stdout=numbered_ids())
    history = AtuinHistory()
    history.store_string("\\e")
    history.store_string("SELECT edited")
    history.end(False)
    assert ends(fake) == [("0", "id1"), ("1", "id2")]


@pytest.mark.parametrize(("success", "exit_code"), [(True, "0"), (False, "1")])
def test_end_finishes_the_stored_entry_with_its_outcome(fake_atuin, success, exit_code):
    fake = fake_atuin(stdout="01a040f0cb257242b718f2ab411b84ce\n")
    history = AtuinHistory()
    history.store_string("SELECT 1")
    history.end(success)
    assert fake.calls[1] == ["atuin", "history", "end", "--exit", exit_code, "--", "01a040f0cb257242b718f2ab411b84ce"]
    # Not waited for, since it may sync.
    assert fake.kwargs["start_new_session"] is True

    history.end(success)  # nothing is pending any more
    assert len(fake.calls) == 2


def test_no_end_when_start_fails(fake_atuin):
    fake = fake_atuin(returncode=1)
    history = AtuinHistory()
    history.store_string("SELECT 1")
    history.end(True)
    assert len(fake.calls) == 1


def test_legacy_entries_are_appended_not_imported(fake_atuin):
    fake_atuin(stdout="new query\0")

    class Legacy:
        def load_history_strings(self):
            return ["old query"]

    assert list(AtuinHistory(legacy=Legacy()).load_history_strings()) == ["new query", "old query"]


def test_falls_back_to_legacy_when_atuin_fails(fake_atuin):
    fake_atuin(returncode=1)

    class Legacy:
        def load_history_strings(self):
            return ["old query"]

    assert list(AtuinHistory(legacy=Legacy()).load_history_strings()) == ["old query"]


def test_survives_a_missing_or_wedged_executable(fake_atuin):
    for boom in (FileNotFoundError("atuin"), subprocess.TimeoutExpired("atuin", 5)):
        fake_atuin(raises=boom)
        history = AtuinHistory()
        assert list(history.load_history_strings()) == []
        history.store_string("SELECT 1")  # must not raise


def test_picker_seeds_the_query_and_keeps_stdout_on_the_terminal(fake_atuin):
    fake = fake_atuin(stderr="SELECT 1\n")
    buffer = FakeBuffer("SEL")
    assert search_history(FakeEvent(buffer), "pgcli") is True

    argv = fake.calls[0]
    assert argv[:3] == ["atuin", "search", "-i"]
    assert "--shell-up-key-binding" in argv
    # The picker ignores --author, so scoping rides on the synthetic session.
    assert argv[argv.index("--filter-mode") + 1] == "session"
    assert "--author" not in argv
    assert fake.env["ATUIN_SESSION"] == session_id("pgcli")
    assert fake.env["ATUIN_QUERY"] == "SEL"
    # Opened from the up-arrow, atuin takes this over --filter-mode.
    assert fake.env["ATUIN_FILTER_MODE_SHELL_UP_KEY_BINDING"] == "session"
    # The TUI draws on stdout; only the pick is captured.
    assert fake.kwargs["stdout"] is None
    assert fake.kwargs["stderr"] is subprocess.PIPE

    assert buffer.text == "SELECT 1"
    assert buffer.accepted is False


def test_enter_accept_marker_runs_the_query(fake_atuin):
    fake_atuin(stderr=ACCEPT_PREFIX + "SELECT 1\n")
    buffer = FakeBuffer()
    search_history(FakeEvent(buffer), "pgcli")
    assert buffer.text == "SELECT 1"
    assert buffer.accepted is True


def test_escape_leaves_the_buffer_alone(fake_atuin):
    fake_atuin(stderr="\n")
    buffer = FakeBuffer("SELECT untouched")
    assert search_history(FakeEvent(buffer), "pgcli") is True
    assert buffer.text == "SELECT untouched"
    assert buffer.accepted is False


def test_reports_failure_so_the_caller_can_fall_back(fake_atuin):
    fake_atuin(raises=FileNotFoundError("atuin"))
    buffer = FakeBuffer("SELECT 1")
    assert search_history(FakeEvent(buffer), "pgcli") is False
    assert buffer.text == "SELECT 1"


def test_error_exit_is_a_failure_not_a_pick(fake_atuin):
    fake_atuin(stderr="Error: unexpected argument\n", returncode=2)
    buffer = FakeBuffer("SELECT 1")
    assert search_history(FakeEvent(buffer), "pgcli") is False
    assert buffer.text == "SELECT 1"
    assert buffer.accepted is False


def _up_bindings(cli):
    from pgcli.key_bindings import pgcli_bindings

    return [b for b in pgcli_bindings(cli).bindings if any(str(k) == "Keys.Up" for k in b.keys)]


def test_shipped_default_leaves_atuin_off():
    """The packaged pgclirc must not turn the feature on for existing users."""
    from pgcli.config import get_config

    c = get_config(None)
    assert c["main"].as_bool("atuin_history") is False
    assert c["main"].as_bool("atuin_keys") is False


def test_up_is_not_rebound_unless_enabled():
    from pgcli.main import PGCli

    # Set explicitly, so this does not depend on the shipped default.
    cli = PGCli()
    cli.atuin_keys = False
    cli.atuin_history = False
    assert _up_bindings(cli) == []


def test_up_is_rebound_when_enabled(monkeypatch):
    from pgcli import atuin as atuin_module
    from pgcli.main import PGCli

    monkeypatch.setattr(atuin_module, "is_available", lambda: True)
    cli = PGCli()
    cli.atuin_keys = True
    cli.atuin_history = True
    assert len(_up_bindings(cli)) == 1


@pytest.mark.parametrize(("searching", "active"), [(False, True), (True, False)])
def test_up_is_left_to_reverse_search_while_searching(monkeypatch, searching, active):
    """In Ctrl-R search, Up must not open atuin and paste into the search box."""
    from types import SimpleNamespace

    from prompt_toolkit.application.current import set_app

    from pgcli import atuin as atuin_module
    from pgcli.main import PGCli

    monkeypatch.setattr(atuin_module, "is_available", lambda: True)
    cli = PGCli()
    cli.atuin_keys = True
    cli.atuin_history = True
    [binding] = _up_bindings(cli)

    app = SimpleNamespace(layout=SimpleNamespace(is_searching=searching), current_buffer=SimpleNamespace(selection_state=None))
    with set_app(app):
        assert binding.filter() is active


def test_up_not_rebound_when_atuin_history_is_off(monkeypatch):
    """atuin's picker would show an empty list, so the key must stay on history."""
    from pgcli import atuin as atuin_module
    from pgcli.main import PGCli

    monkeypatch.setattr(atuin_module, "is_available", lambda: True)
    cli = PGCli()
    cli.atuin_keys = True
    cli.atuin_history = False
    assert _up_bindings(cli) == []


def test_run_cli_ends_each_entry_with_its_querys_outcome(monkeypatch, fake_atuin):
    from pgcli import atuin as atuin_module
    from pgcli import main as main_module
    from pgcli.main import MetaQuery, PGCli, PgCliQuitError

    fake = fake_atuin(stdout=numbered_ids())
    monkeypatch.setattr(atuin_module, "is_available", lambda: True)
    cli = PGCli()
    cli.atuin_history = True
    cli.less_chatty = True

    class FakePrompt:
        def __init__(self, history):
            self.history = history
            self.texts = ["SELECT 1", "SELECT 1", "SELECT bad", "\\e", "\\q"]

        def prompt(self, default=None):
            # \e prompts again with the edited SQL as the default.
            if default is not None:
                text = default
            elif not self.texts:
                raise EOFError
            else:
                text = self.texts.pop(0)
            # prompt_toolkit stores the line on accept, before it is run, and
            # skips a repeat of the previous entry (Buffer.append_to_history).
            strings = self.history.get_strings()
            if not strings or strings[-1] != text:
                self.history.append_string(text)
            return text

    def execute_command(text):
        if text == "\\q":
            raise PgCliQuitError
        return MetaQuery(query=text, successful=text == "SELECT 1")

    monkeypatch.setattr(cli, "refresh_completions", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_build_cli", FakePrompt)
    monkeypatch.setattr(cli, "execute_command", execute_command)
    monkeypatch.setattr(cli, "_check_ongoing_transaction_and_allow_quitting", lambda: True)
    monkeypatch.setattr(main_module.special, "open_external_editor", lambda filename, sql, editor: ("SELECT edited", None))
    cli.run_cli()

    assert starts(fake) == ["SELECT 1", "SELECT bad", "\\e", "SELECT edited", "\\q"]
    # The repeated SELECT 1 was not stored, so it ends nothing; \e's own entry is
    # finished when the edited SQL is stored, and that SQL gets the outcome.
    assert ends(fake) == [("0", "id1"), ("1", "id2"), ("0", "id3"), ("1", "id4"), ("0", "id5")]
