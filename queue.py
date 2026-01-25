"""Command queue for ESP32 Bulb Relay with rate limiting."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .const import MIN_COMMAND_INTERVAL

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class QueuedCommand:
    """A command waiting to be executed."""
    
    coro_func: Callable[[], Awaitable[Any]]
    future: asyncio.Future
    
    async def execute(self) -> Any:
        """Execute the command and set the result."""
        try:
            result = await self.coro_func()
            self.future.set_result(result)
            return result
        except Exception as err:
            self.future.set_exception(err)
            raise


class CommandQueue:
    """Queue for rate-limiting commands to an ESP32."""
    
    def __init__(self, min_interval: float = MIN_COMMAND_INTERVAL) -> None:
        """Initialize the command queue."""
        self._queue: asyncio.Queue[QueuedCommand] = asyncio.Queue()
        self._min_interval = min_interval
        self._last_command_time: float = 0
        self._processor_task: asyncio.Task | None = None
        self._running = False
    
    def start(self) -> None:
        """Start the queue processor."""
        if self._running:
            return
        self._running = True
        self._processor_task = asyncio.create_task(self._process_queue())
        _LOGGER.debug("Command queue processor started")
    
    async def stop(self) -> None:
        """Stop the queue processor."""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
            self._processor_task = None
        _LOGGER.debug("Command queue processor stopped")
    
    async def _process_queue(self) -> None:
        """Process commands from the queue with rate limiting."""
        while self._running:
            try:
                # Wait for a command
                command = await self._queue.get()
                
                # Calculate delay needed to respect rate limit
                now = time.monotonic()
                elapsed = now - self._last_command_time
                if elapsed < self._min_interval:
                    delay = self._min_interval - elapsed
                    _LOGGER.debug("Rate limiting: waiting %.3fs before next command", delay)
                    await asyncio.sleep(delay)
                
                # Execute the command
                try:
                    await command.execute()
                except Exception as err:
                    _LOGGER.debug("Command failed: %s", err)
                
                # Update last command time
                self._last_command_time = time.monotonic()
                
                # Mark task as done
                self._queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Error in queue processor: %s", err)
    
    async def enqueue(self, coro_func: Callable[[], Awaitable[T]]) -> T:
        """Add a command to the queue and wait for its result.
        
        Args:
            coro_func: A callable that returns a coroutine (not the coroutine itself).
                      This allows us to delay execution until it's time to run.
        
        Returns:
            The result of the command.
            
        Raises:
            Any exception raised by the command.
        """
        # Ensure processor is running
        if not self._running:
            self.start()
        
        # Create the queued command
        loop = asyncio.get_running_loop()
        command = QueuedCommand(
            coro_func=coro_func,
            future=loop.create_future(),
        )
        
        # Add to queue
        await self._queue.put(command)
        _LOGGER.debug("Command queued, queue size: %d", self._queue.qsize())
        
        # Wait for result
        return await command.future
    
    @property
    def pending_count(self) -> int:
        """Return the number of pending commands."""
        return self._queue.qsize()
