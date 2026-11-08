"""Signal live streams before Uvicorn waits for active HTTP connections."""

import asyncio
import uvicorn


class FrontendServer(uvicorn.Server):
    def run(self, sockets=None):
        # Uvicorn replays SIGINT after graceful cleanup. At the executable
        # boundary this is a normal exit, including in the reload child.
        try:
            return super().run(sockets=sockets)
        except KeyboardInterrupt:
            return None

    async def shutdown(self, sockets=None):
        event = self.lifespan.state.get('shutdown_event')
        if event is not None:
            event.set()
        await super().shutdown(sockets=sockets)


async def read_stream_line(reader, shutdown_event, timeout, cancel_event=None):
    """Interrupt socket reads promptly, retaining a bounded heartbeat interval."""
    read = asyncio.create_task(reader.readline())
    stop = asyncio.create_task(shutdown_event.wait())
    revoked = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    tasks = {read, stop} | ({revoked} if revoked else set())
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if stop in done or revoked in done:
            return None
        if read in done:
            return read.result()
        raise TimeoutError
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
