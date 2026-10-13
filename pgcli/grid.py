"""Lay out a single-column result like ``ls -C``, in a frame that matches the table format.

Mirrors the fork's mycli grid. Only text and NULL values are laid out: numbers,
dates and arrays go through pgcli's formatting settings (decimal_format,
column_date_formats, array braces) in the table path, so those keep the table.
"""

from cli_helpers.utils import filter_style_table, style_field
from prompt_toolkit.utils import get_cwidth
from pygments.token import Token

# Table formats whose single-column results may be laid out as a grid.
# Data formats (csv, tsv, jsonl, ...) keep one value per line.
GRID_FORMATS = frozenset({
    "ascii",
    "ascii_escaped",
    "plain",
    "simple",
    "minimal",
    "grid",
    "fancy_grid",
    "pipe",
    "orgtbl",
    "psql",
    "psql_unicode",
    "rst",
    "github",
    "double",
    "mysql",
    "mysql_unicode",
    "mysql_heavy",
})
GRID_GAP = 2
# top-left, top-right, middle-left, middle-right, bottom-left, bottom-right, horizontal, vertical
ASCII_FRAME = ("+", "+", "+", "+", "+", "+", "-", "|")
# psql draws the rule under the header as |----|, not +----+.
PSQL_FRAME = ("+", "+", "|", "|", "+", "+", "-", "|")
UNICODE_FRAME = ("┌", "┐", "├", "┤", "└", "┘", "─", "│")
FRAMES = {
    "psql": PSQL_FRAME,
    "psql_unicode": UNICODE_FRAME,
    "fancy_grid": UNICODE_FRAME,
    "double": UNICODE_FRAME,
    "mysql_unicode": UNICODE_FRAME,
    "mysql_heavy": UNICODE_FRAME,
}


def _style_text(token, text, style):
    """Style *text* like cli_helpers styles table cells, leaving it plain if the style does not set *token*."""
    if style and filter_style_table(style, token).get(token):
        return style_field(token, text, style)
    return text


def format_as_grid(header, rows, width, missing_value, style=None, table_format="ascii"):
    """Lay out one column's values column-major, like ``ls -C``, in a framed box under the header.

    Returns None when a value is not text or NULL, contains a newline or tab, or
    when fewer than two columns fit in *width*.
    """
    values = []
    nulls = set()
    for i, (value,) in enumerate(rows):
        if value is None:
            value = missing_value
            nulls.add(i)
        elif not isinstance(value, str) or "\n" in value or "\t" in value:
            return None
        values.append(value)
    if len(values) < 2:
        return None

    # The frame takes '| ' on the left and ' |' on the right.
    inner_width = width - 4
    widths = [get_cwidth(v) for v in values]
    header_width = get_cwidth(header)
    if header_width > inner_width:
        return None
    # Every column is at least min(widths) wide, which bounds how many can fit.
    max_cols = min(len(values), (inner_width + GRID_GAP) // (min(widths) + GRID_GAP))
    for ncols in range(max_cols, 1, -1):
        nrows = -(-len(values) // ncols)
        ncols = -(-len(values) // nrows)  # drop columns that would be empty
        col_widths = [max(widths[c * nrows : (c + 1) * nrows]) for c in range(ncols)]
        grid_width = sum(col_widths) + GRID_GAP * (ncols - 1)
        if grid_width <= inner_width:
            break
    else:
        return None

    top_left, top_right, mid_left, mid_right, bottom_left, bottom_right, horizontal, vertical = FRAMES.get(table_format, ASCII_FRAME)
    content_width = max(grid_width, header_width)
    rule = horizontal * (content_width + 2)
    side = _style_text(Token.Output.TableSeparator, vertical, style)

    def framed(text, text_width):
        return f"{side} {text}{' ' * (content_width - text_width)} {side}"

    lines = [
        _style_text(Token.Output.TableSeparator, top_left + rule + top_right, style),
        framed(_style_text(Token.Output.Header, header, style), header_width),
        _style_text(Token.Output.TableSeparator, mid_left + rule + mid_right, style),
    ]
    for r in range(nrows):
        cells = []
        line_width = 0
        for c in range(ncols):
            i = c * nrows + r
            if i >= len(values):
                break
            cell = _style_text(Token.Output.Null, values[i], style) if i in nulls else values[i]
            pad = col_widths[c] - widths[i] + GRID_GAP if c < ncols - 1 else 0
            cells.append(cell + " " * pad)
            line_width += widths[i] + pad
        lines.append(framed("".join(cells), line_width))
    lines.append(_style_text(Token.Output.TableSeparator, bottom_left + rule + bottom_right, style))
    return lines
