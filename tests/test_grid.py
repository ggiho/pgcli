import pytest
from cli_helpers.utils import strip_ansi
from prompt_toolkit.utils import get_cwidth
from pygments.style import Style
from pygments.token import Token

from pgcli.grid import format_as_grid
from pgcli.main import OutputSettings, format_output


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
