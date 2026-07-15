"""Internal helper: one persistent background event loop, shared by every
OpenSandboxBackend instance in the process.

OpenSandbox's async client can pin internal connection state to the event
loop it was created on. Calling asyncio.run() per operation opens and closes
a new loop each time, which can break that client after the first call.
Routing every await through a single long-lived loop avoids that class of
bug entirely.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Any


class _BackgroundLoop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="deepagents-opensandbox-loop", daemon=True
        )
        self._thread.start()
        atexit.register(self.stop)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)


# Module-level singleton: every backend instance shares one loop/thread.
_shared_loop: _BackgroundLoop | None = None


def get_loop() -> _BackgroundLoop:
    global _shared_loop
    if _shared_loop is None:
        _shared_loop = _BackgroundLoop()
    return _shared_loop
