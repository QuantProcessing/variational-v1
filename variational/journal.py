"""Async-queue-backed JSONL writer. Never blocks the caller.

Trade-path callers use `emit()` which is sync and O(1). Serialization
and disk writes happen on a background drainer task. On queue overflow
events are dropped and counted; they are never dropped silently without
a counter.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class EventJournal:
    def __init__(
        self,
        path: Path,
        max_queue: int = 10000,
        batch_ms: int = 50,
        batch_size: int = 100,
    ) -> None:
        self.path = path
        self.max_queue = max_queue
        self.batch_ms = batch_ms
        self.batch_size = batch_size
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._drainer: asyncio.Task[None] | None = None
        self._stop = False
        self._dropped = 0
        self._written = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def written_count(self) -> int:
        return self._written

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = asyncio.Queue(maxsize=self.max_queue)
        self._stop = False
        self._drainer = asyncio.create_task(self._drain_forever())

    async def stop(self) -> None:
        self._stop = True
        if self._drainer is not None:
            try:
                await asyncio.wait_for(self._drainer, timeout=5.0)
            except asyncio.TimeoutError:
                self._drainer.cancel()

    def emit(self, event: dict[str, Any]) -> None:
        if self._queue is None:
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1

    async def _drain_forever(self) -> None:
        assert self._queue is not None
        batch: list[dict[str, Any]] = []
        batch_timeout = self.batch_ms / 1000.0
        last_flush = time.monotonic()
        while True:
            if self._stop and self._queue.empty() and not batch:
                return
            # Always await with a floor so we yield to the loop; never busy-spin.
            if batch:
                remaining = batch_timeout - (time.monotonic() - last_flush)
                wait = max(0.001, remaining)
            else:
                wait = batch_timeout
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=wait)
                batch.append(event)
            except asyncio.TimeoutError:
                pass

            should_flush = bool(batch) and (
                len(batch) >= self.batch_size
                or (time.monotonic() - last_flush) >= batch_timeout
                or self._stop
            )
            if should_flush:
                await self._flush(batch)
                batch = []
                last_flush = time.monotonic()

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        lines = "".join(json.dumps(ev, ensure_ascii=True, default=str) + "\n" for ev in batch)
        await asyncio.to_thread(self._append_text, self.path, lines)
        self._written += len(batch)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(text)


async def _self_test() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "journal_test.jsonl"
    journal = EventJournal(tmp, max_queue=16, batch_ms=10, batch_size=4)
    await journal.start()

    for i in range(10):
        journal.emit({"i": i, "msg": f"hello {i}"})
    # Overflow test: fill past max_queue without awaiting drainer.
    for i in range(10, 100):
        journal.emit({"i": i})
    await asyncio.sleep(0.2)
    await journal.stop()

    lines = tmp.read_text(encoding="utf-8").splitlines()
    assert journal.written_count == len(lines), f"written={journal.written_count} lines={len(lines)}"
    assert journal.written_count + journal.dropped_count >= 100
    assert lines[0].startswith('{"i": 0'), lines[0]
    print(f"EventJournal OK: written={journal.written_count} dropped={journal.dropped_count} path={tmp}")


if __name__ == "__main__":
    asyncio.run(_self_test())
