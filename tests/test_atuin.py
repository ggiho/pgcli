import subprocess

import pytest

from pgcli.atuin import ACCEPT_PREFIX, AtuinHistory, search_history, session_id


class FakeAtuin:
    """Stands in for the atuin executable, recording argv and replaying stdout.

    `--cmd-only` searches and the interactive picker get separate canned output.
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
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture
def fake_atuin(monkeypatch):
    def install(**kwargs):
        fake = FakeAtuin(**kwargs)
        monkeypatch.setattr(subprocess, "run", fake)
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
    fake = fake_atuin(stdout="SELECT 2\0SELECT 1\0SELECT 1\0")
    assert list(AtuinHistory().load_history_strings()) == ["SELECT 2", "SELECT 1", "SELECT 1"]
    argv = fake.calls[0]
    assert argv[:2] == ["atuin", "search"]
    assert argv[argv.index("--author") + 1] == "pgcli"
    # Duplicates drive frequency-based auto-suggest ranking.
    assert "--include-duplicates" in argv
    # atuin prints oldest first; without --reverse the up-arrow order inverts.
    assert "--reverse" in argv
    assert "--cmd-only" in argv


def test_load_preserves_multiline_entries(fake_atuin):
    fake_atuin(stdout="SELECT a,\n       b\n  FROM t\0SELECT 1\0")
    assert list(AtuinHistory().load_history_strings()) == ["SELECT a,\n       b\n  FROM t", "SELECT 1"]


def test_store_records_with_author_and_no_fake_exit(fake_atuin):
    fake = fake_atuin(stdout="01a040f0cb257242b718f2ab411b84ce\n")
    AtuinHistory(author="pgcli-test").store_string("SELECT 1")
    assert fake.calls == [["atuin", "history", "start", "--author", "pgcli-test", "SELECT 1"]]
    # `history end` needs an exit code we cannot know at store time.
    assert not any("end" in call for call in fake.calls)
    # Recording and the picker must agree on the session.
    assert fake.env["ATUIN_SESSION"] == session_id("pgcli-test")


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
    assert search_history(FakeEvent(buffer), "pgcli", up_key_binding=True) is True

    argv = fake.calls[0]
    assert argv[:3] == ["atuin", "search", "-i"]
    assert "--shell-up-key-binding" in argv
    # The picker ignores --author, so scoping rides on the synthetic session.
    assert argv[argv.index("--filter-mode") + 1] == "session"
    assert "--author" not in argv
    assert fake.env["ATUIN_SESSION"] == session_id("pgcli")
    assert fake.env["ATUIN_QUERY"] == "SEL"
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

    # Set explicitly: PGCli() reads the real user config, which may enable these.
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


def test_up_not_rebound_when_atuin_history_is_off(monkeypatch):
    """atuin's picker would show an empty list, so the key must stay on history."""
    from pgcli import atuin as atuin_module
    from pgcli.main import PGCli

    monkeypatch.setattr(atuin_module, "is_available", lambda: True)
    cli = PGCli()
    cli.atuin_keys = True
    cli.atuin_history = False
    assert _up_bindings(cli) == []
