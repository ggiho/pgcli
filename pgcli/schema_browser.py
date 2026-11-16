"""A table-first explorer with structured columns and separate SQL definitions."""

from __future__ import annotations

from functools import partial
import threading
from typing import Any, Callable, Protocol

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import Dimension, DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.selection import SelectionType
from prompt_toolkit.styles import BaseStyle, Style, merge_styles
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Label, SearchToolbar, TextArea

from pygments.lexers.sql import PostgresLexer
from pgcli.schema_clipboard import copy_text
from pgcli.schema_model import Column, Structure


class Metadata(Protocol):
    def tables(self) -> list[tuple[str, str, str]]: ...
    def describe(self, schema: str, name: str) -> Structure: ...
    def select_sql(self, schema: str, name: str) -> str: ...
    def qualified_name(self, schema: str, name: str) -> str: ...


def fit(value: str, width: int) -> str:
    """Fit display cells, preserving complete values in the detail panel."""
    value = ''.join(c if c.isprintable() else ' ' for c in value)
    if get_cwidth(value) <= width:
        return value + ' ' * (width - get_cwidth(value))
    result = ''
    for char in value:
        if get_cwidth(result + char) > max(0, width - 1):
            break
        result += char
    return result + '…' + ' ' * max(0, width - 1 - get_cwidth(result))


class SchemaBrowser:
    def __init__(
        self, metadata: Metadata, *, style: BaseStyle | None = None, input_stream: Input | None = None, output_stream: Output | None = None
    ) -> None:
        self.metadata = metadata
        self.objects: list[tuple[str, str, str]] = []
        self.matches: list[tuple[str, str, str]] = []
        self.index = 0
        self.column_index = 0
        self.section = 'Columns'
        self.structure: Structure | None = None
        self.status = 'Loading tables…'
        self.generation = 0
        self.detail_width = 70
        self._metadata_lock = threading.Lock()
        self.cache: dict[tuple[str, str], Structure] = {}
        self.search = TextArea(height=1, multiline=False, prompt=' Find table  ')
        self.column_search = TextArea(height=1, multiline=False, prompt=' Find column  ')
        self.sql_search = SearchToolbar()
        self.sql = TextArea(
            read_only=True, scrollbar=True, lexer=PygmentsLexer(PostgresLexer), search_field=self.sql_search, focus_on_click=True
        )
        self.details = TextArea(read_only=True, height=3, style='class:schema.muted')
        self.control = FormattedTextControl(
            self._names, focusable=True, show_cursor=False, get_cursor_position=lambda: Point(0, self.index)
        )
        self.list_window = Window(self.control, wrap_lines=False)
        self.columns_control = FormattedTextControl(
            self._columns, focusable=True, show_cursor=False, get_cursor_position=lambda: Point(0, self.column_index)
        )
        self.columns_window = Window(self.columns_control, wrap_lines=False)
        self.index_text = TextArea(read_only=True, scrollbar=True, lexer=PygmentsLexer(PostgresLexer), focus_on_click=True)
        self.tabs = {
            name: Button(name, partial(self.show_section, name), width=len(name) + 4, left_symbol=' ', right_symbol=' ')
            for name in ['Columns', 'Indexes', 'SQL']
        }
        self.select_button = Button('SELECT draft', partial(self.choose, 'select'), width=16)
        self.insert_button = Button('Insert name', partial(self.choose, 'insert'), width=15)
        self.cancel_button = Button('Close', self.close)
        self.copy_button = Button('Copy', self.copy_text, width=10)
        self.copy_all_button = Button('Copy all', partial(self.copy_text, True), width=12)
        self.app: Application[tuple[str, str] | None] = Application(
            layout=Layout(DynamicContainer(self._body), focused_element=self.search),
            key_bindings=self._bindings(),
            full_screen=True,
            mouse_support=True,
            style=merge_styles([
                style or Style([]),
                Style.from_dict({
                    '': 'bg:default',
                    'schema.muted': 'fg:ansibrightblack',
                    'schema.title': 'bold',
                    'schema.key': 'fg:ansiyellow bold',
                    'schema.type': 'fg:ansicyan',
                    'schema.selected': 'reverse',
                    'schema.current': 'bold',
                    'button': 'bg:default',
                    'button.focused': 'reverse',
                    'tab.active': 'bold underline',
                    'cursor-line': 'underline',
                    'selected': 'reverse',
                }),
            ]),
            input=input_stream,
            output=output_stream,
        )
        for area in (self.sql, self.index_text):
            area.window.cursorline = Condition(partial(self.app.layout.has_focus, area))
        self.app.ttimeoutlen = self.app.timeoutlen = 0.05
        self.search.buffer.on_text_changed += self._filter
        self.column_search.buffer.on_text_changed += self._filter_columns

    def _body(self) -> HSplit:
        size = get_app().output.get_size()
        wide = size.columns >= 90
        left_width = min(38, max(25, size.columns // 3))
        self.detail_width = max(18, size.columns - left_width - 3 if wide else size.columns - 2)
        left = HSplit(
            [
                self.search,
                Label(lambda: f' {len(self.matches)} {"table" if len(self.matches) == 1 else "tables"}', style='class:schema.muted'),
                self.list_window,
            ],
            width=left_width if wide else None,
            height=None if wide else Dimension(min=3, max=min(7, max(3, len(self.matches) + 2))),
        )
        title = '.'.join(self.matches[self.index][:2]) if self.matches else 'No table selected'
        for name, tab in self.tabs.items():
            tab.window.style = lambda name=name, tab=tab: (
                'class:button.focused'
                if self.app.layout.has_focus(tab)
                else 'class:tab.active'
                if name == self.section
                else 'class:schema.muted'
            )
        content: HSplit | TextArea
        if self.section == 'Columns':
            rows: list[Any] = [self.column_search, Window(FormattedTextControl(self._column_header), height=1), self.columns_window]
            if size.rows >= 20:
                rows.append(self.details)
            content = HSplit(rows)
        elif self.section == 'Indexes':
            content = self.index_text
        else:
            content = HSplit([self.sql, self.sql_search])
        right = HSplit(
            [Label(title, style='class:schema.title'), VSplit(list(self.tabs.values()), padding=1, height=1), content],
            width=self.detail_width,
        )
        body: VSplit | HSplit
        if wide:
            body = VSplit([left, Window(width=1, char='│', style='class:schema.muted'), right], padding=1)
        else:
            body = HSplit([left, Window(height=1, char='─', style='class:schema.muted'), right])
        return HSplit([
            Label(' Database  /  Browse', style='class:schema.title'),
            body,
            Label(lambda: self.status, style='class:schema.muted'),
            Label(lambda: self._hint()),
            VSplit(
                (
                    [self.copy_button, self.copy_all_button, self.cancel_button]
                    if self.section in ('SQL', 'Indexes')
                    else [self.select_button, self.insert_button, self.cancel_button]
                ),
                padding=2,
                height=1,
            ),
        ])

    def _hint(self) -> str:
        if self.app.layout.has_focus(self.search):
            return ' ↑↓ choose · Enter inspect · Tab actions · Esc close'
        if self.app.layout.has_focus(self.control):
            return ' j/k choose · / find · Enter inspect · Esc search'
        if self.section in ('SQL', 'Indexes'):
            return ' hjkl move · v select · y copy · Shift+arrows select · Esc back'
        if self.app.layout.has_focus(self.columns_control):
            return ' j/k inspect · / find column · Esc tables · Tab views'
        return ' ↑↓ inspect · Esc tables · Tab views/actions'

    def _names(self) -> StyleAndTextTuples:
        return [
            (
                (
                    'class:schema.selected'
                    if self.app.layout.has_focus(self.search) or self.app.layout.has_focus(self.control)
                    else 'class:schema.current'
                )
                if i == self.index
                else '',
                f' {schema}.{name}\n',
                partial(self._table_mouse, i),
            )
            for i, (schema, name, _kind) in enumerate(self.matches)
        ] or [('', ' No matches')]

    def _table_mouse(self, index: int, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_UP:
            self.index = index
            self.app.layout.focus(self.control)
            self.show_selected()
        elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.move_table(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)

    def _visible_columns(self) -> list[Column]:
        words = self.column_search.text.casefold().split()
        return [c for c in self.structure.columns if all(w in f'{c.name} {c.datatype}'.casefold() for w in words)] if self.structure else []

    def _widths(self) -> tuple[int, int]:
        available = max(8, self.detail_width - 16)
        name = min(32, max(4, available // 2))
        return name, min(28, available - name)

    def _column_header(self) -> StyleAndTextTuples:
        a, b = self._widths()
        return [('class:schema.muted', f' {fit("COLUMN", a)}  {fit("TYPE", b)}  KEY  NULL\n')]

    def _columns(self) -> StyleAndTextTuples:
        if self.structure is None:
            return [('class:schema.muted', ' ' + self.status)]
        columns = self._visible_columns()
        if not columns:
            return [('class:schema.muted', ' No matching columns')]
        a, b = self._widths()
        parts: StyleAndTextTuples = []
        for i, column in enumerate(columns):
            selected = (
                (
                    'class:schema.selected'
                    if self.app.layout.has_focus(self.column_search) or self.app.layout.has_focus(self.columns_control)
                    else 'class:schema.current'
                )
                if i == self.column_index
                else ''
            )
            handler = partial(self._column_mouse, i)
            parts.extend([
                (selected, f' {fit(column.name, a)}  ', handler),
                (selected or 'class:schema.type', fit(column.datatype, b) + '  ', handler),
                (
                    selected or ('class:schema.key' if column.primary else 'class:schema.muted'),
                    ('PK' if column.primary else '—').ljust(3) + '  ',
                    handler,
                ),
                (selected or 'class:schema.muted', ('yes' if column.nullable else 'no') + '\n', handler),
            ])
        return parts

    def _column_mouse(self, index: int, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.MOUSE_UP:
            self.column_index = index
            self.app.layout.focus(self.columns_control)
            self._update_detail()
        elif event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.move_column(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)

    def _filter_columns(self, *_args: object) -> None:
        self.column_index = 0
        self._update_detail()

    def _update_detail(self) -> None:
        columns = self._visible_columns()
        if columns:
            c = columns[self.column_index]
            text = f'{c.name} · {c.datatype}\nDefault: {c.default if c.default is not None else "—"}\n{c.comment}'
        else:
            text = ''
        self.details.buffer.set_document(Document(text, 0), bypass_readonly=True)
        self.app.invalidate()

    def move_table(self, delta: int) -> None:
        target = max(0, min(self.index + delta, len(self.matches) - 1))
        if target != self.index:
            self.index = target
            self.show_selected()

    def move_column(self, delta: int) -> None:
        self.column_index = max(0, min(self.column_index + delta, len(self._visible_columns()) - 1))
        self._update_detail()

    def _filter(self, *_args: object) -> None:
        words = self.search.text.casefold().split()
        self.matches = [r for r in self.objects if all(w in f'{r[0]}.{r[1]}'.casefold() for w in words)]
        self.index = 0
        self.show_selected()

    def start(self) -> None:
        self._fetch(None, self.metadata.tables)

    def _fetch(self, key: tuple[str, str] | None, operation: Callable[[], Any]) -> None:
        def work() -> None:
            try:
                with self._metadata_lock:
                    if self.app.is_done or (key is not None and (not self.matches or self.matches[self.index][:2] != key)):
                        return
                    result, error = operation(), ''
            except Exception as exc:
                result, error = None, str(exc)
            loop = self.app.loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._received, key, result, error)
                except RuntimeError:
                    pass

        threading.Thread(target=work, daemon=True, name='schema_metadata').start()

    def _received(self, key: tuple[str, str] | None, result: Any, error: str) -> None:
        if self.app.is_done:
            return
        if key is None:
            if error:
                self.status = f'Could not load tables: {error}'
            else:
                self.objects = result
                self._filter()
        else:
            if not error:
                self.cache[key] = result
            if self.matches and self.matches[self.index][:2] == key:
                if error:
                    self.status = f'Could not load structure: {error}'
                else:
                    self._apply(result)
        self.app.invalidate()

    def _apply(self, result: Structure) -> None:
        self.structure = result
        self.column_index = 0
        self.sql.buffer.set_document(Document(result.definition or '-- No SQL definition available.', 0), bypass_readonly=True)
        indexes = '\n\n'.join(f'{i.name}\n  {i.definition}' for i in result.indexes) or 'No indexes'
        self.index_text.buffer.set_document(Document(indexes, 0), bypass_readonly=True)
        self.status = f'{len(result.columns)} columns · {len(result.indexes)} indexes'
        if len(self.objects) >= 2000:
            self.status += ' · first 2,000 objects'
        if self.section == 'SQL' and result.note:
            self.status = result.note
        self._update_detail()

    def show_selected(self) -> None:
        self.generation += 1
        generation = self.generation
        self.structure = None
        self.column_search.text = ''
        self.column_index = 0
        self.details.buffer.set_document(Document(''), bypass_readonly=True)
        self.sql.buffer.set_document(Document(''), bypass_readonly=True)
        self.index_text.buffer.set_document(Document(''), bypass_readonly=True)
        if not self.matches:
            self.status = 'No matching tables'
            return
        key = self.matches[self.index][:2]
        if key in self.cache:
            self._apply(self.cache[key])
            return
        self.status = 'Loading structure…'

        def later() -> None:
            if generation == self.generation and not self.app.is_done:
                self._fetch(key, lambda: self.metadata.describe(*key))

        if self.app.loop is not None:
            self.app.loop.call_later(0.15, later)

    def show_section(self, name: str) -> None:
        self.section = name
        target = self.column_search if name == 'Columns' else self.index_text if name == 'Indexes' else self.sql
        self.app.layout.focus(target)
        if self.structure:
            self.status = (
                self.structure.note
                if name == 'SQL' and self.structure.note
                else f'{len(self.structure.columns)} columns · {len(self.structure.indexes)} indexes'
            )
        self.app.invalidate()

    def _text_area(self) -> TextArea:
        return self.sql if self.section == 'SQL' else self.index_text

    def copy_text(self, whole: bool = False) -> None:
        area = self._text_area()
        buffer = area.buffer
        if whole:
            text = buffer.text
        elif buffer.selection_state is not None:
            _document, data = buffer.document.cut_selection()
            text = data.text
        else:
            text = buffer.document.current_line
        if not text:
            self.status = 'Nothing selected to copy.'
            return
        try:
            copy_text(text)
        except Exception as error:
            self.status = f'Copy failed: {error}'
            return
        self.app.clipboard.set_text(text)
        self.status = 'Copied all text.' if whole else 'Copied selection.' if buffer.selection_state is not None else 'Copied line.'

    def choose(self, action: str) -> None:
        if self.matches:
            schema, name, _kind = self.matches[self.index]
            value = self.metadata.select_sql(schema, name) if action == 'select' else self.metadata.qualified_name(schema, name)
            self.app.exit(result=(action, value))

    def close(self) -> None:
        self.generation += 1
        self.app.exit()

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        tables = Condition(lambda: self.app.layout.has_focus(self.search) or self.app.layout.has_focus(self.control))
        columns = Condition(lambda: self.app.layout.has_focus(self.column_search) or self.app.layout.has_focus(self.columns_control))

        reading = Condition(lambda: self.section in ('SQL', 'Indexes') and self.app.layout.has_focus(self._text_area()))

        @kb.add('h', filter=reading)
        def text_left(_event: KeyPressEvent) -> None:
            self._text_area().buffer.cursor_left()

        @kb.add('l', filter=reading)
        def text_right(_event: KeyPressEvent) -> None:
            self._text_area().buffer.cursor_right()

        @kb.add('j', filter=reading)
        def text_down(_event: KeyPressEvent) -> None:
            self._text_area().buffer.cursor_down()

        @kb.add('k', filter=reading)
        def text_up(_event: KeyPressEvent) -> None:
            self._text_area().buffer.cursor_up()

        @kb.add('v', filter=reading)
        def text_select(_event: KeyPressEvent) -> None:
            buffer = self._text_area().buffer
            if buffer.selection_state is not None:
                buffer.exit_selection()
            else:
                buffer.start_selection(SelectionType.CHARACTERS)

        @kb.add('y', filter=reading)
        def text_copy(_event: KeyPressEvent) -> None:
            self.copy_text()

        @kb.add('j', filter=Condition(lambda: self.app.layout.has_focus(self.control)))
        @kb.add('down', filter=tables)
        def down(_event: KeyPressEvent) -> None:
            self.move_table(1)

        @kb.add('k', filter=Condition(lambda: self.app.layout.has_focus(self.control)))
        @kb.add('up', filter=tables)
        def up(_event: KeyPressEvent) -> None:
            self.move_table(-1)

        @kb.add('j', filter=Condition(lambda: self.app.layout.has_focus(self.columns_control)))
        @kb.add('down', filter=columns)
        def column_down(_event: KeyPressEvent) -> None:
            self.move_column(1)

        @kb.add('k', filter=Condition(lambda: self.app.layout.has_focus(self.columns_control)))
        @kb.add('up', filter=columns)
        def column_up(_event: KeyPressEvent) -> None:
            self.move_column(-1)

        @kb.add('f', filter=Condition(lambda: self.app.layout.has_focus(self.control) or self.app.layout.has_focus(self.columns_control)))
        @kb.add('/', filter=Condition(lambda: self.app.layout.has_focus(self.control) or self.app.layout.has_focus(self.columns_control)))
        def search(_event: KeyPressEvent) -> None:
            self.app.layout.focus(self.search if self.app.layout.has_focus(self.control) else self.column_search)

        @kb.add('h', filter=Condition(lambda: self.app.layout.has_focus(self.columns_control)))
        def left(_event: KeyPressEvent) -> None:
            self.app.layout.focus(self.control)

        @kb.add('l', filter=Condition(lambda: self.app.layout.has_focus(self.control)))
        def right(_event: KeyPressEvent) -> None:
            if self.matches:
                self.show_section('Columns')
                self.app.layout.focus(self.columns_control)

        @kb.add('enter', filter=tables)
        def inspect(_event: KeyPressEvent) -> None:
            if self.matches:
                self.show_section('Columns')

        @kb.add('enter', filter=Condition(lambda: self.app.layout.has_focus(self.column_search)))
        def inspect_column(_event: KeyPressEvent) -> None:
            self.app.layout.focus(self.columns_control)

        @kb.add('c-f', filter=Condition(lambda: self.app.layout.has_focus(self.sql)))
        def find_sql(_event: KeyPressEvent) -> None:
            from prompt_toolkit.search import start_search

            start_search(self.sql.control)

        @kb.add('tab')
        def next_field(event: KeyPressEvent) -> None:
            focus_next(event)

        @kb.add('s-tab')
        def previous_field(event: KeyPressEvent) -> None:
            focus_previous(event)

        @kb.add('escape', filter=Condition(lambda: not self.app.layout.has_focus(self.sql_search)))
        def back(_event: KeyPressEvent) -> None:
            if reading() and self._text_area().buffer.selection_state is not None:
                self._text_area().buffer.exit_selection()
            elif self.app.layout.has_focus(self.search):
                self.close()
            else:
                self.app.layout.focus(self.search)

        @kb.add('c-c')
        def cancel(_event: KeyPressEvent) -> None:
            if reading() and self._text_area().buffer.selection_state is not None:
                self.copy_text()
            else:
                self.close()

        return kb


def browse_schema(metadata: Metadata, *, style: BaseStyle | None = None) -> tuple[str, str] | None:
    ui = SchemaBrowser(metadata, style=style)
    return ui.app.run(pre_run=ui.start)
