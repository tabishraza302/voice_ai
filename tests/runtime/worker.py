"""Cross-process spool worker used by the sequence-allocation test."""

from __future__ import annotations

import asyncio
import uuid

from bot.runtime.outbox import EventSpool


def append_worker(path: str, session_id: str, count: int) -> list[int]:
    async def run() -> list[int]:
        spool = EventSpool(path)
        await spool.open()
        try:
            sequences: list[int] = []
            for index in range(count):
                record = await spool.append_event(
                    session_id=session_id,
                    event_type="transcript.segment",
                    payload={
                        "speaker": "user",
                        "final_text": f"worker-{uuid.uuid4().hex}-{index}",
                        "start_ms": index,
                        "end_ms": index + 1,
                    },
                )
                sequences.append(record.sequence)
            return sequences
        finally:
            await spool.close()

    return asyncio.run(run())


__all__ = ["append_worker"]
