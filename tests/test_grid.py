from collections import namedtuple
from unittest import mock

import pytest
from cli_helpers.utils import strip_ansi
from prompt_toolkit.utils import get_cwidth
from pygments.style import Style
from pygments.token import Token

from pgcli.grid import GRID_FORMATS, format_as_grid
from pgcli.main import OutputSettings, PGCli, format_output


class FakeCursor:
    """Rows plus the rowcount a psycopg cursor carries, without a server."""

    def __init__(self, rows):
        self.rows = rows
        self.rowcount = len(rows)

    def __iter__(self):
        return iter(self.rows)


def test_format_as_grid_fills_columns_top_to_bottom_like_ls():
    rows = [(name,) for name in ["alpha", "b", "charlie", "d", "echo"]]

    # Three columns of two rows need 5 + 2 + 7 + 2 + 4 = 20 cells plus 4 for the
    # frame; anything narrower falls back to two columns of three rows.
    assert format_as_grid("datname", rows, 24, "<null>", table_format="psql") == [
        "+----------------------+",
        "| datname              |",
        "|----------------------|",
        "| alpha  charlie  echo |",
        "| b      d             |",
        "+----------------------+",
    ]
    assert format_as_grid("datname", rows, 23, "<null>", table_format="psql") == [
        "+---------------+",
        "| datname       |",
        "|---------------|",
        "| alpha    d    |",
        "| b        echo |",
        "| charlie       |",
        "+---------------+",
    ]


@pytest.mark.parametrize(
    ("table_format", "rule"),
    [("ascii", "+------+"), ("psql_unicode", "├──────┤")],
)
def test_format_as_grid_matches_the_table_formats_frame(table_format, rule):
    assert format_as_grid("h", [("a",), ("b",)], 80, "<null>", table_format=table_format)[2] == rule


def test_format_as_grid_widens_the_frame_for_a_long_header():
    assert format_as_grid("tablename_long", [("a",), ("b",)], 80, "<null>") == [
        "+----------------+",
        "| tablename_long |",
        "+----------------+",
        "| a  b           |",
        "+----------------+",
    ]


def test_format_as_grid_styles_header_separators_and_nulls():
    class GridStyle(Style):
        styles = {Token.Output.Header: "bold", Token.Output.TableSeparator: "#888888", Token.Output.Null: "italic"}

    lines = format_as_grid("h", [("a",), (None,)], 80, "<null>", style=GridStyle)

    assert [strip_ansi(line) for line in lines] == ["+-----------+", "| h         |", "+-----------+", "| a  <null> |", "+-----------+"]
    assert all(line != strip_ansi(line) for line in lines)


def test_format_as_grid_measures_wide_characters():
    lines = format_as_grid("h", [("한글",), ("a",)], 80, "<null>")

    assert lines[3] == "| 한글  a |"
    assert len({get_cwidth(line) for line in lines}) == 1


@pytest.mark.parametrize(
    "rows",
    [[(1,), (2,)], [(["a"],), ("b",)], [("a\nb",), ("c",)], [("a\tb",), ("c",)], [("only",)], [("x" * 80,), ("y",)]],
)
def test_format_as_grid_returns_none_for_values_it_cannot_lay_out(rows):
    assert format_as_grid("h", rows, 80, "<null>") is None


def test_format_output_lays_out_single_column_as_grid_when_width_is_given():
    settings = OutputSettings(table_format="psql", dcmlfmt="d", floatfmt="g", grid_width=80)
    results = format_output("Title", [("a",), ("b",), ("c",)], ["name"], "SELECT 3", settings)

    assert list(results) == ["Title", "+---------+", "| name    |", "|---------|", "| a  b  c |", "+---------+", "SELECT 3"]


@pytest.mark.parametrize(
    "settings",
    [
        OutputSettings(table_format="psql", dcmlfmt="d", floatfmt="g"),
        OutputSettings(table_format="psql", dcmlfmt="d", floatfmt="g", grid_width=80, expanded=True),
        OutputSettings(table_format="csv", dcmlfmt="d", floatfmt="g", grid_width=80),
    ],
)
def test_format_output_keeps_table_without_width_or_for_vertical_and_data_formats(settings):
    lines = list(format_output(None, [("a",), ("b",)], ["name"], None, settings))

    assert "a  b" not in "\n".join(lines)


def test_format_output_keeps_table_for_more_than_one_column():
    settings = OutputSettings(table_format="psql", dcmlfmt="d", floatfmt="g", grid_width=80)

    assert list(format_output(None, [("a", "b")], ["h1", "h2"], None, settings))[2] == "|----+----|"


@pytest.mark.parametrize("table_format", sorted(GRID_FORMATS))
def test_format_output_lays_out_grid_for_boxed_formats(table_format):
    settings = OutputSettings(table_format=table_format, dcmlfmt="d", floatfmt="g", grid_width=80)

    assert any("a  b" in line for line in format_output(None, [("a",), ("b",)], ["name"], None, settings))


@pytest.mark.parametrize("table_format", ["plain", "simple", "minimal", "github", "pipe", "rst", "orgtbl"])
def test_format_output_keeps_table_for_formats_without_a_box(table_format):
    settings = OutputSettings(table_format=table_format, dcmlfmt="d", floatfmt="g", grid_width=80)

    assert not any("a  b" in line for line in format_output(None, [("a",), ("b",)], ["name"], None, settings))


@pytest.mark.parametrize("header", ["QUERY PLAN", "query plan"])
def test_format_output_keeps_a_typed_explain_one_node_per_line(header):
    settings = OutputSettings(table_format="psql", dcmlfmt="d", floatfmt="g", grid_width=80)
    plan = [("Seq Scan on t  (cost=0.00..1.01 rows=1 width=4)",), ("  Filter: (a = 1)",)]

    lines = list(format_output(None, plan, [header], "EXPLAIN", settings))

    assert lines[3:5] == ["| Seq Scan on t  (cost=0.00..1.01 rows=1 width=4) |", "|   Filter: (a = 1)                               |"]


@pytest.mark.parametrize("rows", [[("a",), ("b",), ("c",)], [(1,), (2,), (3,)]], ids=["grid", "grid declined"])
def test_format_output_keeps_the_row_count_redshift_leaves_out_of_the_status(rows):
    settings = OutputSettings(table_format="psql", dcmlfmt="d", floatfmt="g", grid_width=80)

    assert list(format_output(None, FakeCursor(rows), ["name"], "SELECT", settings))[-1] == "SELECT 3"


termsize = namedtuple("termsize", ["rows", "columns"])


def test_grid_is_off_while_a_log_file_is_written(tmp_path):
    """\\log-file gets the same output as the screen, and a file keeps one value per line."""
    cli = PGCli()
    cli.prompt_app = mock.Mock()
    cli.prompt_app.output.get_size.return_value = termsize(rows=24, columns=80)
    cli.pgexecute = mock.Mock()
    cli.pgexecute.run.side_effect = lambda *args, **kwargs: [
        (None, FakeCursor([("a",), ("b",), ("c",)]), ["name"], "SELECT 3", "SELECT", True, False)
    ]

    output, _ = cli._evaluate_command("SELECT")
    assert "| a  b  c |" in output

    cli.log_file = str(tmp_path / "pgcli.log")
    output, _ = cli._evaluate_command("SELECT")
    assert "| a  b  c |" not in output
