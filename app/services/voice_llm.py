"""
app/services/voice_llm.py
---------------------------
Async bridge from a voice socket to ai_router.stream(). The router and the
LLM adapters are synchronous (they also serve Celery), so each generation
runs in a worker thread with its own DB session and hands chunks to the
event loop through a queue.

cancel() sets a flag the router checks between chunks, so a cancelled
generation stops at the next chunk and its HTTP stream is closed; the
socket stops reading at once either way.
"""
from __future__ import annotations

import asyncio
import threading
from typing import AsyncIterator

from app.core.db import SessionLocal
from app.providers.llm import LLMError
from app.services import ai_router


class LLMStream:
    def __init__(self, *, user_id: str, task: str, system: str, user_prompt: str, max_tokens: int = 600,
                 free_only: bool = False, ref: str | None = None, timeout: float | None = None,
                 images: list[tuple[str, bytes]] | None = None, first_token_timeout: float | None = None):
        self.user_id = user_id
        self.task = task
        self.system = system
        self.user_prompt = user_prompt
        self.max_tokens = max_tokens
        self.free_only = free_only
        self.ref = ref
        self.timeout = timeout
        self.first_token_timeout = first_token_timeout
        self.images = images  # held in memory for this one call only
        self.outcome = ai_router.StreamOutcome()
        self._cancel = threading.Event()
        self._queue: asyncio.Queue | None = None

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _work(self, loop: asyncio.AbstractEventLoop) -> None:
        def put(item) -> None:
            loop.call_soon_threadsafe(self._queue.put_nowait, item)

        db = SessionLocal()
        try:
            for chunk in ai_router.stream(db, user_id=self.user_id, task=self.task, system=self.system,
                                          user_prompt=self.user_prompt, outcome=self.outcome,
                                          max_tokens=self.max_tokens, ref=self.ref, free_only=self.free_only,
                                          timeout=self.timeout, cancel=self._cancel, images=self.images,
                                          first_token_timeout=self.first_token_timeout):
                put(("chunk", chunk))
                if self._cancel.is_set():
                    break
            put(("done", None))
        except ai_router.NoModelAvailable as e:
            put(("no_model", e))
        except LLMError as e:
            put(("error", e))
        except Exception as e:  # noqa: BLE001 — surfaced to the socket as a provider error
            put(("error", LLMError(f"{type(e).__name__}: {e}"[:200])))
        finally:
            self.images = None
            db.close()

    async def chunks(self) -> AsyncIterator[str]:
        """Yields text chunks. Raises NoModelAvailable / LLMError like the router."""
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        loop.run_in_executor(None, self._work, loop)
        while True:
            kind, value = await self._queue.get()
            if self._cancel.is_set():
                return
            if kind == "chunk":
                yield value
            elif kind == "done":
                return
            else:
                raise value
