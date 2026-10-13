"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially -- one at a time.
Priority callbacks (stop:*) bypass the queue and run immediately so they can
interrupt the currently-running handler.
"""

import asyncio
from typing import Any, Awaitable

from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor that lets priority callbacks bypass sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority callbacks (``stop:*``): we just ``await coroutine`` -- runs
    immediately.
    For everything else: we acquire ``_sequential_lock`` first -- only one
    runs at a time.

    A stop callback arrives while a text handler holds the lock -> stop
    callback runs concurrently -> fires the ``asyncio.Event`` -> the watcher
    task inside ``execute_command()`` calls ``client.interrupt()`` -> Claude
    stops -> ``run_command()`` returns -> handler finishes -> lock released.
    """

    # "ask:" belongs here for the same reason "stop:" does, only more so: the
    # turn is *blocked* waiting for that tap, so queueing it behind the lock
    # that turn holds would deadlock the bot until the question times out.
    _PRIORITY_PREFIXES = ("stop:", "ask:")

    def __init__(self) -> None:
        # High limit so priority callbacks are never blocked by semaphore
        super().__init__(max_concurrent_updates=256)
        self._sequential_lock = asyncio.Lock()

    @classmethod
    def _is_priority_callback(cls, update: object) -> bool:
        """Whether this update must skip the queue.

        Two kinds do. A `stop:` or `ask:` tap, because the turn holding the
        lock is the very thing waiting on it. And a typed message while a
        question is open, for the same reason: it *is* the answer, so making
        it wait for the turn to end would deliver it minutes late, as a new
        request, to a question that had already given up.
        """
        if not isinstance(update, Update):
            return False

        cb = update.callback_query
        if cb is not None and cb.data is not None:
            return cb.data.startswith(cls._PRIORITY_PREFIXES)

        message = update.effective_message
        if message is not None and message.text and message.via_bot is None:
            from .utils.ask_user import pending_count

            return pending_count() > 0
        return False

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        """Process an update, applying sequential lock for non-priority updates."""
        if self._is_priority_callback(update):
            # Run immediately -- no sequential lock
            await coroutine
        else:
            # One at a time for everything else
            async with self._sequential_lock:
                await coroutine

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
