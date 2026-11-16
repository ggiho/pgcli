"""Search database objects and inspect metadata without executing result SQL."""

from __future__ import annotations

from functools import partial
import threading
from typing import Any, Callable, Protocol

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import Dimension, DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import BaseStyle, Style, merge_styles
from prompt_toolkit.widgets import Button, Frame, Label, SearchToolbar, TextArea


class Metadata(Protocol):
    def tables(self) -> list[tuple[str, str, str]]: ...
    def describe(self, schema: str, name: str) -> str: ...
    def select_sql(self, schema: str, name: str) -> str: ...
    def qualified_name(self, schema: str, name: str) -> str: ...


class SchemaBrowser:
    def __init__(
        self, metadata: Metadata, *, style: BaseStyle | None = None, input_stream: Input | None = None, output_stream: Output | None = None
    ) -> None:
        self.metadata = metadata
        self.objects: list[tuple[str, str, str]] = []
        self.matches: list[tuple[str, str, str]] = []
        self.index = 0
        self.status = 'Loading tables…'
        self.generation = 0
        self._metadata_lock = threading.Lock()
        self.cache: dict[tuple[str, str], str] = {}
        self.search = TextArea(height=1, multiline=False, prompt='Tables: ')
        self.column_search = SearchToolbar(text_if_not_searching='Ctrl+F in structure: find column or text')
        self.preview = TextArea(read_only=True, scrollbar=True, search_field=self.column_search, focus_on_click=True)
        self.control = FormattedTextControl(
            self._names, focusable=True, show_cursor=False, get_cursor_position=lambda: Point(0, self.index)
        )
        self.list_window = Window(self.control)
        self.select_button = Button('SELECT', partial(self.choose, 'select'))
        self.insert_button = Button('Insert name', partial(self.choose, 'insert'), width=15)
        self.cancel_button = Button('Cancel', self.close)
        self.app: Application[tuple[str, str] | None] = Application(
            layout=Layout(DynamicContainer(self._body), focused_element=self.search),
            key_bindings=self._bindings(),
            full_screen=True,
            mouse_support=True,
            style=merge_styles([
                style or Style([]),
                Style.from_dict({
                    '': 'bg:default',
                    'frame.border': 'fg:ansibrightblack',
                    'button.focused': 'reverse',
                    'schema.selected': 'reverse',
                }),
            ]),
            input=input_stream,
            output=output_stream,
        )
        self.app.ttimeoutlen = 0.05
        self.app.timeoutlen = 0.05
        self.search.buffer.on_text_changed += self._filter

    def _body(self) -> HSplit:
        wide = get_app().output.get_size().columns >= 80
        left = Frame(
            self.list_window,
            title=f'Tables · {len(self.matches)}',
            width=Dimension(weight=1) if wide else None,
            height=None if wide else Dimension(max=7),
        )
        right = Frame(self.preview, title='Structure / columns', width=Dimension(weight=2) if wide else None)
        panes = VSplit([left, right], padding=1) if wide else HSplit([left, right])
        return HSplit([
            self.search,
            panes,
            self.column_search,
            Label(lambda: self.status),
            Label('↑↓ select · Tab structure/actions · Enter SELECT draft · Esc close'),
            VSplit([self.select_button, self.insert_button, self.cancel_button], padding=2),
        ])

    def _names(self) -> list[tuple[str, str]]:
        return [
            ('class:schema.selected' if i == self.index else '', f' {schema}.{name} [{kind}]\n')
            for i, (schema, name, kind) in enumerate(self.matches)
        ] or [('', ' No matching tables')]

    def _filter(self, *_args: object) -> None:
        words = self.search.text.casefold().split()
        self.matches = [row for row in self.objects if all(w in f'{row[0]}.{row[1]} {row[2]}'.casefold() for w in words)]
        self.index = 0
        self.show_selected()

    def start(self) -> None:
        self._fetch(None, lambda: self.metadata.tables())

    def _fetch(self, key: tuple[str, str] | None, operation: Callable[[], Any]) -> None:
        def work() -> None:
            try:
                with self._metadata_lock:
                    if key is not None and (self.app.is_done or not self.matches or self.matches[self.index][:2] != key):
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
                self.preview.buffer.set_document(
                    Document(result if not error else f'Metadata unavailable: {error}', 0), bypass_readonly=True
                )
                self.status = self._scope()
        self.app.invalidate()

    def _scope(self) -> str:
        return 'Up to 2,000 accessible tables/views · SELECT only fills the query prompt'

    def show_selected(self) -> None:
        self.generation += 1
        generation = self.generation
        if not self.matches:
            self.preview.buffer.set_document(Document(''), bypass_readonly=True)
            self.status = self._scope()
            return
        schema, name, _kind = self.matches[self.index]
        key = schema, name
        if key in self.cache:
            self.preview.buffer.set_document(Document(self.cache[key], 0), bypass_readonly=True)
            self.status = self._scope()
            return
        self.preview.buffer.set_document(Document('Loading structure…'), bypass_readonly=True)
        self.status = self._scope()

        # Debounce browsing so holding an arrow does not open a connection per row.
        def later() -> None:
            if generation == self.generation and not self.app.is_done:
                self._fetch(key, lambda: self.metadata.describe(*key))

        if self.app.loop is not None:
            self.app.loop.call_later(0.2, later)

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
        selecting = Condition(lambda: self.app.layout.has_focus(self.search) or self.app.layout.has_focus(self.control))

        @kb.add('down', filter=selecting)
        def down(_event: KeyPressEvent) -> None:
            self.index = min(self.index + 1, max(0, len(self.matches) - 1))
            self.show_selected()

        @kb.add('up', filter=selecting)
        def up(_event: KeyPressEvent) -> None:
            self.index = max(0, self.index - 1)
            self.show_selected()

        @kb.add('enter', filter=selecting)
        def select(_event: KeyPressEvent) -> None:
            self.choose('select')

        @kb.add('c-f', filter=Condition(lambda: self.app.layout.has_focus(self.preview)))
        def find(_event: KeyPressEvent) -> None:
            from prompt_toolkit.search import start_search

            start_search(self.preview.control)

        @kb.add('tab')
        def next_field(event: KeyPressEvent) -> None:
            focus_next(event)

        @kb.add('s-tab')
        def previous_field(event: KeyPressEvent) -> None:
            focus_previous(event)

        @kb.add('escape', filter=Condition(lambda: not self.app.layout.has_focus(self.column_search)))
        @kb.add('c-c')
        def cancel(_event: KeyPressEvent) -> None:
            self.close()

        return kb


def browse_schema(metadata: Metadata, *, style: BaseStyle | None = None) -> tuple[str, str] | None:
    ui = SchemaBrowser(metadata, style=style)
    return ui.app.run(pre_run=ui.start)
