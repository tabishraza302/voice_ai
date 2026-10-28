"""Opt-in, loopback-only end-to-end SmallWebRTC acceptance test.

Run explicitly with ``RUN_LIVE_VOICE=1 uv run pytest -s tests/test_voice_live_integration.py``.
The test uses a silent synthetic microphone track and sends benign text turns
over the same ``chat`` data channel as the Studio client. It never captures a
real microphone or plays audio through a device.
"""

from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pytest


pytestmark = [pytest.mark.live_voice, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _check_live_voice_prereq() -> None:
    if os.getenv("RUN_LIVE_VOICE") != "1":
        if os.environ.get("VERIFY_REQUIRED") == "1":
            pytest.fail("required qualification prerequisite unavailable: RUN_LIVE_VOICE not set")
        pytest.skip("set RUN_LIVE_VOICE=1 to run the opt-in local SmallWebRTC acceptance test")


@pytest.mark.asyncio
async def test_local_voice_roundtrip_for_english_and_hindi() -> None:
    """The local browser path returns transcripts and synthesized audio."""
    import aiohttp
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack

    base_url = os.environ.get("VOICE_AI_TEST_AGENT_URL", "http://127.0.0.1:7860")
    request_timeout = aiohttp.ClientTimeout(total=6)
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    silent_input = AudioStreamTrack()
    peer.addTrack(silent_input)
    peer.addTransceiver("video", direction="sendrecv")
    peer.addTransceiver("video", direction="sendonly")
    channel = peer.createDataChannel("chat", ordered=True)

    channel_open = asyncio.Event()
    audio_track_found = asyncio.Event()
    received: asyncio.Queue[dict] = asyncio.Queue()
    incoming_audio = None
    audio_receive_task: asyncio.Task | None = None
    keepalive_task: asyncio.Task | None = None

    @channel.on("open")
    def on_channel_open() -> None:
        channel_open.set()

    @channel.on("message")
    def on_channel_message(message: str | bytes) -> None:
        if not isinstance(message, str):
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            received.put_nowait(payload)

    @peer.on("track")
    def on_remote_track(track) -> None:  # noqa: ANN001
        nonlocal incoming_audio, audio_receive_task
        if track.kind == "audio":
            incoming_audio = track
            audio_track_found.set()

            async def receive_audio() -> int:
                peak = 0
                while True:
                    frame = await track.recv()
                    peak = max(peak, int(np.max(np.abs(frame.to_ndarray()))))
                    if peak >= 128:
                        return peak

            audio_receive_task = asyncio.create_task(receive_audio())

    async def wait_for_turn(text: str) -> str:
        user_seen = False
        async with asyncio.timeout(55):
            while True:
                message = await received.get()
                if message.get("type") != "transcript":
                    continue
                if message.get("role") == "user" and message.get("text") == text:
                    user_seen = True
                    continue
                if message.get("role") == "assistant" and user_seen:
                    reply = message.get("text")
                    if isinstance(reply, str) and reply.strip():
                        return reply

    async def keep_connection_alive() -> None:
        while True:
            if channel.readyState == "open":
                channel.send("ping")
            await asyncio.sleep(1)

    try:
        async with aiohttp.ClientSession(
            timeout=request_timeout,
            trust_env=False,
        ) as session:
            async with session.get(f"{base_url}/api/agents") as response:
                response.raise_for_status()
                agents = await response.json()
            agent = next(
                (
                    item
                    for item in agents
                    if item.get("slug") == "bilingual-assistant"
                    and item.get("is_preset") is True
                ),
                None,
            )
            assert agent is not None, "local API has no bilingual-assistant preset"

            offer = await peer.createOffer()
            await peer.setLocalDescription(offer)
            async with asyncio.timeout(5):
                while peer.iceGatheringState != "complete":
                    await asyncio.sleep(0.025)

            async with session.post(
                f"{base_url}/api/offer",
                json={
                    "sdp": peer.localDescription.sdp,
                    "type": peer.localDescription.type,
                    "request_data": {"agent_id": agent["id"]},
                },
            ) as response:
                response.raise_for_status()
                answer = await response.json()
            assert isinstance(answer.get("pc_id"), str)
            await peer.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )

            async with asyncio.timeout(15):
                await channel_open.wait()
                await audio_track_found.wait()
            channel.send(
                json.dumps(
                    {
                        "type": "signalling",
                        "message": {
                            "type": "trackStatus",
                            "receiver_index": 0,
                            "enabled": True,
                        },
                    }
                )
            )
            channel.send(
                json.dumps(
                    {
                        "label": "rtvi-ai",
                        "type": "client-ready",
                        "id": "client-ready",
                        "data": {
                            "version": "1.0.0",
                            "about": {"library": "voice-ai-live-test", "platform": "python"},
                        },
                    }
                )
            )
            keepalive_task = asyncio.create_task(keep_connection_alive())

            turns = (
                "Answer in one short English sentence: say hello and mention a blue bird.",
                "कृपया हिन्दी में एक छोटा वाक्य बोलें: आज मौसम अच्छा है।",
            )
            replies = []
            for text in turns:
                channel.send(json.dumps({"type": "send-text", "text": text}))
                replies.append(await wait_for_turn(text))

            assert len(replies) == 2
            assert all(reply.strip() for reply in replies)
            assert any(char.isascii() and char.isalpha() for char in replies[0]), (
                "English prompt did not produce an English-script reply: "
                f"{replies[0]!r}"
            )
            assert not any("\u0900" <= char <= "\u097f" for char in replies[0]), (
                f"English reply unexpectedly contained Devanagari: {replies[0]!r}"
            )
            assert any("\u0900" <= char <= "\u097f" for char in replies[1]), (
                f"Hindi prompt did not produce a Devanagari reply: {replies[1]!r}"
            )
            assert audio_receive_task is not None, "remote audio output track was not received"
            peak = await asyncio.wait_for(audio_receive_task, timeout=10)
            assert peak >= 128, "received audio track contained only silence"
    finally:
        if keepalive_task is not None:
            keepalive_task.cancel()
            await asyncio.gather(keepalive_task, return_exceptions=True)
        if audio_receive_task is not None and not audio_receive_task.done():
            audio_receive_task.cancel()
            await asyncio.gather(audio_receive_task, return_exceptions=True)
        if channel.readyState != "closed":
            channel.close()
        if incoming_audio is not None:
            incoming_audio.stop()
        silent_input.stop()
        await peer.close()
