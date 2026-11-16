from __future__ import annotations

import asyncio
from unittest.mock import Mock

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from pgcli.schema_browser import SchemaBrowser


@pytest.mark.parametrize('width', [50, 100])
def test_browse_filters_and_stages_select_without_running_it(width: int) -> None:
    provider = Mock()
    provider.tables.return_value = [('public', 'orders', 'TABLE'), ('public', 'customers', 'TABLE')]
    provider.describe.return_value = 'id integer PRIMARY KEY\namount numeric'
    provider.select_sql.return_value = 'SELECT * FROM "public"."orders" LIMIT 100;'

    class Output(DummyOutput):
        def get_size(self) -> Size:
            return Size(rows=24, columns=width)

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=Output())
            provider.tables.assert_not_called()
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            for _ in range(200):
                if ui.objects:
                    break
                await asyncio.sleep(0.01)
            assert len(ui.objects) == 2
            pipe.send_text('orders')
            for _ in range(200):
                if ui.preview.text.startswith('id integer'):
                    break
                await asyncio.sleep(0.01)
            assert ui.matches == [('public', 'orders', 'TABLE')]
            assert ui.preview.text == 'id integer PRIMARY KEY\namount numeric'
            pipe.send_text('\r')
            return await asyncio.wait_for(task, 3)

    assert asyncio.run(scenario()) == ('select', provider.select_sql.return_value)
    provider.select_sql.assert_called_once_with('public', 'orders')


def test_metadata_error_stays_in_browser_and_can_close() -> None:
    provider = Mock()
    provider.tables.side_effect = OSError('metadata unavailable')

    async def scenario():
        with create_pipe_input() as pipe:
            ui = SchemaBrowser(provider, input_stream=pipe, output_stream=DummyOutput())
            task = asyncio.create_task(ui.app.run_async(pre_run=ui.start))
            for _ in range(200):
                if 'unavailable' in ui.status:
                    break
                await asyncio.sleep(0.01)
            assert 'unavailable' in ui.status
            pipe.send_text('\x1b')
            assert await asyncio.wait_for(task, 1) is None

    asyncio.run(scenario())
