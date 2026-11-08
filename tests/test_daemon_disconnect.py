import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings


@pytest.mark.parametrize('failure', ['read', 'write', 'close'])
def test_disconnected_rpc_client_cleanup(failure, caplog):
    async def scenario():
        server = DaemonServer(DaemonSettings(_env_file=None))
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"jsonrpc":"2.0","id":"test","method":"ping"}\n')
        reader.feed_eof()
        writer = Mock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock(side_effect=BrokenPipeError('peer closed'))
        if failure == 'read':
            reader.readline = AsyncMock(side_effect=ConnectionResetError('peer reset'))
        elif failure == 'write':
            writer.drain.side_effect = BrokenPipeError('peer closed')
        await asyncio.wait_for(server.handle_client(reader, writer), 2)
        writer.close.assert_called()
        assert not server.events.subscribers

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert not caplog.records


def test_subscription_write_failure_closes_and_unregisters_client(caplog):
    async def scenario():
        server = DaemonServer(DaemonSettings(_env_file=None))
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"jsonrpc":"2.0","id":"sub","method":"events.subscribe","params":{"topics":["network"]}}\n')
        writer = Mock()
        acknowledged = asyncio.Event()

        def write(payload):
            if b'"method":"event"' in payload:
                raise BrokenPipeError('subscriber closed')
            acknowledged.set()

        writer.write.side_effect = write
        writer.close.side_effect = reader.feed_eof
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock(side_effect=BrokenPipeError('subscriber closed'))
        task = asyncio.create_task(server.handle_client(reader, writer))
        await asyncio.wait_for(acknowledged.wait(), 2)
        server.events.publish('network.link.changed', {})
        await asyncio.wait_for(task, 2)
        assert not server.events.subscribers

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert not caplog.records
