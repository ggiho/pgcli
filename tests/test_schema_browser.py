from __future__ import annotations

import asyncio
from unittest.mock import Mock

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from pgcli.schema_browser import SchemaBrowser, fit
from pgcli.schema_model import Column, Index, Structure


async def until(predicate) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('UI did not reach expected state')


def fixture_provider() -> Mock:
    provider = Mock()
    provider.tables.return_value = [('public', 'orders', 'TABLE'), ('public', 'customers', 'TABLE')]
    provider.describe.return_value = Structure(
        [Column('id', 'bigint', False, True), Column('amount', 'numeric(20,2)', True, False, '0', 'Total order amount')],
        [Index('orders_pk', 'CREATE UNIQUE INDEX orders_pk ON orders(id)')],
        'CREATE TABLE orders (id bigint PRIMARY KEY, amount numeric(20,2));',
    )
    provider.select_sql.return_value = 'SELECT * FROM "public"."orders" LIMIT 100;'
    return provider


@pytest.mark.parametrize('width', [50, 100])
def test_search_inspect_columns_sql_and_draft_without_execution(width: int) -> None:
    provider = fixture_provider()

    class Output(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=26, columns=width)

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=Output())
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            await until(lambda: bool(ui.objects))
            pipe.send_text('orders\r')
            await until(lambda: ui.structure is not None and ui.app.layout.has_focus(ui.column_search))
            assert not task.done()  # Enter inspects, it does not return or execute a SELECT.
            assert ui.matches == [('public', 'orders', 'TABLE')]
            pipe.send_text('amount\r')
            await until(lambda: ui.app.layout.has_focus(ui.columns_control))
            assert [c.name for c in ui._visible_columns()] == ['amount']
            assert 'Total order amount' in ui.details.text
            ui.show_section('Indexes')
            assert 'orders_pk' in ui.index_text.text
            ui.show_section('SQL')
            assert ui.sql.text == provider.describe.return_value.definition
            pipe.send_text('\x1b')
            await until(lambda: ui.app.layout.has_focus(ui.search))
            assert ui.search.text == 'orders'
            ui.choose('select')
            return await asyncio.wait_for(task, 3)

    assert asyncio.run(scenario()) == ('select', provider.select_sql.return_value)
    provider.select_sql.assert_called_once_with('public', 'orders')


def test_metadata_error_stays_visible_and_can_close() -> None:
    provider = fixture_provider()
    provider.tables.side_effect = OSError('metadata unavailable')

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=DummyOutput())
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            await until(lambda: 'unavailable' in ui.status)
            pipe.send_text('\x1b')
            assert await asyncio.wait_for(task, 1) is None

    asyncio.run(scenario())


@pytest.mark.parametrize('width,height', [(120, 30), (80, 24), (50, 24)])
def test_column_grid_and_actions_remain_visible(width: int, height: int) -> None:
    provider = fixture_provider()

    class Output(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=height, columns=width)

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=Output())
            checked = []

            def rendered(_app):
                if not ui.structure or checked:
                    return
                info = ui.columns_window.render_info
                assert info and info.window_height >= 3
                assert ui.search.window.render_info.window_height == 1
                assert ui.select_button.window.render_info.window_height == 1
                assert ui.column_search.window.render_info.window_height == 1
                cells = ui.app.renderer.last_rendered_screen.data_buffer
                lines = [''.join(cell.char for _, cell in sorted(row.items())) for _, row in sorted(cells.items())]
                visible = '\n'.join(lines)
                assert 'COLUMN' in visible and 'TYPE' in visible and 'PK' in visible and 'NULL' in visible
                assert 'BASE TABLE' not in visible and 'CREATE TABLE' not in visible
                checked.append(True)
                ui.close()

            ui.app.after_render += rendered
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            assert await asyncio.wait_for(task, 3) is None
            assert checked

    asyncio.run(scenario())


def test_width_fitting_preserves_wide_character_boundaries() -> None:
    from prompt_toolkit.utils import get_cwidth

    for width in [4, 8, 15]:
        assert get_cwidth(fit('한글_very_long_column', width)) == width
    assert '\n' not in fit('a\nb', 8)


def test_stale_structure_does_not_replace_new_selection() -> None:
    provider = fixture_provider()

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=DummyOutput())
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            await until(lambda: ui.structure is not None)
            ui.search.text = 'customers'
            old = Structure([Column('wrong', 'text', True)])
            ui._received(('public', 'orders'), old, '')
            assert ui.structure is None
            await until(lambda: ui.structure is not None)
            assert ui.structure.columns[0].name == 'id'
            ui.close()
            await task

    asyncio.run(scenario())


def test_keyboard_tabs_sql_search_and_back_preserve_column_filter() -> None:
    provider = fixture_provider()

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=DummyOutput())
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            await until(lambda: bool(ui.objects))
            pipe.send_text('orders\ramount')
            await until(lambda: ui.structure is not None and ui.column_search.text == 'amount')
            pipe.send_text('\x1b[Z\r')  # Previous focus is the SQL tab.
            await until(lambda: ui.section == 'SQL')
            pipe.send_text('\x06PRIMARY\r')
            await until(lambda: ui.sql.buffer.cursor_position > 0)
            pipe.send_text('\x1b')
            await until(lambda: ui.app.layout.has_focus(ui.search))
            assert ui.search.text == 'orders'
            pipe.send_text('\r')
            await until(lambda: ui.app.layout.has_focus(ui.column_search))
            assert ui.column_search.text == 'amount'
            assert [c.name for c in ui._visible_columns()] == ['amount']
            ui.app.layout.focus(ui.insert_button)
            provider.qualified_name.return_value = '"public"."orders"'
            pipe.send_text('\r')
            return await asyncio.wait_for(task, 3)

    assert asyncio.run(scenario()) == ('insert', '"public"."orders"')


@pytest.mark.parametrize('section', ['Indexes', 'SQL'])
def test_detail_views_use_the_available_width(section: str) -> None:
    class Output(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=26, columns=120)

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(fixture_provider(), input_stream=pipe, output_stream=Output())
            checked = []

            def rendered(_app):
                if not ui.structure or checked:
                    return
                if ui.section != section:
                    ui.show_section(section)
                    return
                widget = ui.sql if section == 'SQL' else ui.index_text
                assert widget.window.render_info.window_width >= 75
                checked.append(True)
                ui.close()

            ui.app.after_render += rendered
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            assert await asyncio.wait_for(task, 3) is None
            assert checked

    asyncio.run(scenario())
