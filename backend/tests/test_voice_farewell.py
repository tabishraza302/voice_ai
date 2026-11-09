import asyncio

import pytest
from pipecat.frames.frames import InterruptionFrame
from voice_pipeline_harness import (
    BACKCHANNEL_TEXT,
    CALLER_TEXT,
    COMMAND_REPLY_TEXT,
    COMMAND_TEXT,
    EXTENDED_FAREWELL_TEXT,
    FAREWELL_TEXT,
    ORDINARY_TEXT,
    OUTSIDE_ACK_REPLY_TEXT,
    RunningVoicePipeline,
    verified_environment,
)


@pytest.mark.anyio
async def test_duplicate_farewell_not_repeated_when_already_delivered(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="duplicate-farewell",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for the call to conclude
        await asyncio.wait_for(
            pipeline.connection.call_completed.wait(),
            timeout=3,
        )

        # Normal shutdown flushes writes without private writer synchronization
        await pipeline.close(close_client=False)

        # Observe no repeated spoken synthesis was queued for the farewell message
        assert not pipeline.tts.synthesis_requested.is_set(), (
            "TTSSpeakFrame was queued for farewell even though it was already spoken and delivered"
        )
        assert pipeline.tts.spoken_requests == [], (
            f"Expected no spoken synthesis requests, got {pipeline.tts.spoken_requests}"
        )

        # Observe saved transcript read via existing session API contains the farewell exactly once
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        farewell_messages = [
            m for m in messages
            if m.get("role") == "assistant" and m.get("content") == FAREWELL_TEXT
        ]
        assert len(farewell_messages) == 1, (
            f"Expected exactly 1 farewell message in saved transcript, got {len(farewell_messages)}: {messages}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_extended_farewell_is_synthesized_captioned_and_saved(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="extended-farewell",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for the call to conclude
        await asyncio.wait_for(
            pipeline.connection.call_completed.wait(),
            timeout=3,
        )

        # Normal shutdown flushes writes without private writer synchronization
        await pipeline.close(close_client=False)

        # Observe that the new/extended farewell synthesis was requested through fake TTS
        assert pipeline.tts.synthesis_requested.is_set(), (
            "TTSSpeakFrame was expected for extended farewell containing new info"
        )
        assert EXTENDED_FAREWELL_TEXT in pipeline.tts.spoken_requests, (
            f"Expected {EXTENDED_FAREWELL_TEXT} in spoken requests, got {pipeline.tts.spoken_requests}"
        )

        # Observe final caption was sent for the extended farewell
        assert pipeline.connection.extended_farewell_caption.is_set(), (
            "Extended farewell caption was not sent over WebRTC data channel"
        )

        # Observe saved transcript read via existing session API contains the extended farewell message
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        extended_messages = [
            m for m in messages
            if m.get("role") == "assistant" and m.get("content") == EXTENDED_FAREWELL_TEXT
        ]
        assert len(extended_messages) == 1, (
            f"Expected exactly 1 extended farewell message in saved transcript, got {len(extended_messages)}: {messages}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_natural_farewell_not_cut_off_before_audio_starts(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="natural-farewell-delayed",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for external normal synthesis request and partial caption milestone
        await asyncio.wait_for(
            pipeline.tts.normal_synthesis_requested.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.connection.assistant_partial_caption.wait(),
            timeout=3,
        )

        # Hold >250ms (400ms) while normal synthesis is pending before releasing audio
        await asyncio.sleep(0.4)

        # Assert call has NOT prematurely completed while waiting for delayed normal farewell audio
        premature_completion = any(
            m.get("type") == "call.completed" for m in pipeline.connection.messages
        )
        assert not premature_completion, (
            "call completed prematurely before normal farewell audio began playing"
        )

        # Release normal audio
        pipeline.tts.release_normal_synthesis.set()

        # Observe output completion
        await asyncio.wait_for(
            pipeline.output.assistant_audio_completed.wait(),
            timeout=3,
        )

        # Observe call completion
        await asyncio.wait_for(
            pipeline.connection.call_completed.wait(),
            timeout=3,
        )

        # Output completion must precede call completion
        assert pipeline.events.index("assistant-audio-complete") < pipeline.events.index("call-completed"), (
            "call completed before normal farewell audio finished playing"
        )

        # Normal shutdown flushes writes without private writer synchronization
        await pipeline.close(close_client=False)

        # Require no extra explicit TTSSpeak synthesis request since original farewell plays
        assert not pipeline.tts.synthesis_requested.is_set(), (
            "extra explicit TTSSpeakFrame was queued instead of letting normal farewell play"
        )
        assert pipeline.tts.spoken_requests == [], (
            f"Expected no explicit TTSSpeak requests, got {pipeline.tts.spoken_requests}"
        )

        # Read exactly 1 saved farewell via authenticated session API after normal shutdown
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        farewell_messages = [
            m for m in messages
            if m.get("role") == "assistant" and m.get("content") == FAREWELL_TEXT
        ]
        assert len(farewell_messages) == 1, (
            f"Expected exactly 1 farewell message in saved transcript, got {len(farewell_messages)}: {messages}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_prior_ordinary_speech_delayed_does_not_abort_queued_farewell(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="prior-speech-starts-late",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for ordinary speech synthesis request and its partial caption milestone
        await asyncio.wait_for(
            pipeline.tts.normal_synthesis_requested.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.connection.assistant_partial_caption.wait(),
            timeout=3,
        )

        # Await tool result so tool request is definitely armed before ordinary audio starts
        await asyncio.wait_for(
            pipeline.tool_result_received.wait(),
            timeout=3,
        )
        # Release delayed ordinary audio synthesis
        pipeline.tts.release_normal_synthesis.set()

        # Wait for ordinary audio completion AND explicit farewell synthesis request
        await asyncio.wait_for(
            pipeline.output.ordinary_audio_completed.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.tts.synthesis_requested.wait(),
            timeout=3,
        )

        # Hold farewell audio behind start_synthesis for >250ms (400ms); assert no call.completed
        await asyncio.sleep(0.4)
        premature_completion = any(
            m.get("type") == "call.completed" for m in pipeline.connection.messages
        )
        assert not premature_completion, (
            "call completed after prior speech drained before farewell TTS started"
        )

        # Release farewell audio
        pipeline.tts.start_synthesis.set()

        # Require farewell audio completion before call.completed
        await asyncio.wait_for(
            pipeline.output.audio_completed.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.connection.call_completed.wait(),
            timeout=3,
        )
        assert pipeline.events.index("farewell-audio-complete") < pipeline.events.index("call-completed"), (
            "call completed before farewell audio finished playing"
        )

        # Normal shutdown flushes writes without private writer synchronization
        await pipeline.close(close_client=False)

        # Require farewell caption was delivered
        assert pipeline.connection.farewell_caption.is_set(), (
            "Farewell caption was not delivered over WebRTC data channel"
        )

        # Read exactly 1 saved farewell via authenticated session API after normal shutdown
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        farewell_messages = [
            m for m in messages
            if m.get("role") == "assistant" and m.get("content") == FAREWELL_TEXT
        ]
        assert len(farewell_messages) == 1, (
            f"Expected exactly 1 farewell message in saved transcript, got {len(farewell_messages)}: {messages}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_spoken_farewell_order_matches_audio_completion_in_captions_and_transcript(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="prior-speech-starts-late",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for ordinary speech synthesis request and its partial caption milestone
        await asyncio.wait_for(
            pipeline.tts.normal_synthesis_requested.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.connection.assistant_partial_caption.wait(),
            timeout=3,
        )

        # Wait for model tool call result to be received (milestone indicating end_call executed)
        await asyncio.wait_for(
            pipeline.tool_result_received.wait(),
            timeout=3,
        )

        # Release delayed ordinary audio synthesis
        pipeline.tts.release_normal_synthesis.set()

        # Wait for ordinary audio completion AND explicit farewell synthesis request
        await asyncio.wait_for(
            pipeline.output.ordinary_audio_completed.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.tts.synthesis_requested.wait(),
            timeout=3,
        )

        # Release farewell audio
        pipeline.tts.start_synthesis.set()

        # Wait for farewell audio completion and call completion
        await asyncio.wait_for(
            pipeline.output.audio_completed.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.connection.call_completed.wait(),
            timeout=3,
        )

        # Normal shutdown flushes writes without private writer synchronization
        await pipeline.close(close_client=False)

        # Final assistant captions must follow actual spoken audio order: [ORDINARY_TEXT, FAREWELL_TEXT]
        final_assistant_captions = [
            m.get("text")
            for m in pipeline.connection.messages
            if m.get("type") == "caption"
            and m.get("role") == "assistant"
            and m.get("final") is True
        ]
        assert final_assistant_captions == [ORDINARY_TEXT, FAREWELL_TEXT], (
            f"Expected final assistant caption order {[ORDINARY_TEXT, FAREWELL_TEXT]}, got {final_assistant_captions}"
        )

        # Authenticated session API messages must follow actual spoken audio order: [ORDINARY_TEXT, FAREWELL_TEXT]
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        saved_assistant_texts = [
            m.get("content")
            for m in messages
            if m.get("role") == "assistant"
        ]
        assert saved_assistant_texts == [ORDINARY_TEXT, FAREWELL_TEXT], (
            f"Expected saved assistant message order {[ORDINARY_TEXT, FAREWELL_TEXT]}, got {saved_assistant_texts}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_isolated_acknowledgment_during_bot_playback_does_not_queue_model_turn(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="backchannel-during-playback",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for initial assistant speech playback to become actively speaking
        await asyncio.wait_for(
            pipeline.output.assistant_playback_started.wait(),
            timeout=3,
        )

        # Confirm initial model turn was requested for the caller prompt
        assert pipeline.model_turn_requests == [CALLER_TEXT]

        # While assistant audio is actively speaking, deliver an isolated acknowledgment STT ("okay")
        await pipeline.stt.deliver_transcription(BACKCHANNEL_TEXT, speech_final=True)

        # Wait for user final caption milestone confirming "okay" was processed by the pipeline
        await asyncio.wait_for(
            pipeline.connection.ack_final_caption.wait(),
            timeout=3,
        )

        # Bounded negative observation window: ensure no new model turn is queued
        await asyncio.sleep(0.5)

        # Playback must not have been interrupted by the isolated acknowledgment
        assert not any(
            isinstance(frame, InterruptionFrame)
            for frame in pipeline.output.recorded_frames
        ), "playback was interrupted by isolated acknowledgment"

        # The isolated acknowledgment must NOT have queued another assistant/model turn
        assert BACKCHANNEL_TEXT not in pipeline.model_turn_requests, (
            f"isolated acknowledgment {BACKCHANNEL_TEXT!r} queued an unexpected LLM turn: {pipeline.model_turn_requests}"
        )

        # Next, while playback is still active, deliver a meaningful multi-word command ("okay wait a second")
        await pipeline.stt.deliver_transcription(COMMAND_TEXT, speech_final=True)

        # Meaningful command MUST trigger a real model turn
        await asyncio.wait_for(
            pipeline.connection.command_reply_caption.wait(),
            timeout=3,
        )
        assert COMMAND_TEXT in pipeline.model_turn_requests, (
            f"meaningful command {COMMAND_TEXT!r} did not start a model turn: {pipeline.model_turn_requests}"
        )

        # Assert positive command interruption is actually observed in recorded output
        assert any(
            isinstance(frame, InterruptionFrame)
            for frame in pipeline.output.recorded_frames
        ), "positive command interruption was not observed in recorded output"

        # Release assistant playback and cleanly close
        pipeline.tts.finish_assistant_playback.set()
        await pipeline.close(close_client=False)

        # Authenticated session API must preserve user utterances
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        user_messages = [m.get("content") for m in messages if m.get("role") == "user"]
        assert BACKCHANNEL_TEXT in user_messages, (
            f"Expected {BACKCHANNEL_TEXT!r} in saved user messages: {user_messages}"
        )
        assert COMMAND_TEXT in user_messages, (
            f"Expected {COMMAND_TEXT!r} in saved user messages: {user_messages}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_isolated_acknowledgment_delayed_fallback_after_bot_audio_ends(monkeypatch) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="backchannel-during-playback",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for initial assistant speech playback to reach output transport
        await asyncio.wait_for(
            pipeline.output.assistant_playback_started.wait(),
            timeout=3,
        )

        # Deliver isolated acknowledgment STT with speech_final=False while playback is active
        await pipeline.stt.deliver_transcription(BACKCHANNEL_TEXT, speech_final=False)

        # Wait for external observation of user partial caption confirming receipt
        await asyncio.wait_for(
            pipeline.connection.ack_partial_caption.wait(),
            timeout=3,
        )

        # Deliver VAD stop through external boundary; this triggers the 400ms fallback timer
        await pipeline.stt.deliver_vad_stop()

        # Release assistant playback so it drains and stops BEFORE the 400ms fallback fires
        pipeline.tts.finish_assistant_playback.set()
        await asyncio.wait_for(
            pipeline.output.assistant_playback_completed.wait(),
            timeout=3,
        )

        # Wait for acknowledgment final caption milestone after the fallback flush completes
        await asyncio.wait_for(
            pipeline.connection.ack_final_caption.wait(),
            timeout=3,
        )

        # Bounded negative observation window: ensure no model turn was queued for the backchannel
        await asyncio.sleep(0.1)

        # The acknowledgment started during playback must NOT have queued a model turn even though audio drained first
        assert BACKCHANNEL_TEXT not in pipeline.model_turn_requests, (
            f"Acknowledgment started during bot playback incorrectly became a new model turn after audio drained: {pipeline.model_turn_requests}"
        )

        # Verify same isolated acknowledgment spoken as a NEW utterance OUTSIDE playback DOES start a normal model turn
        pipeline.connection.ack_final_caption.clear()
        await pipeline.stt.deliver_transcription(BACKCHANNEL_TEXT, speech_final=True)

        await asyncio.wait_for(
            pipeline.connection.outside_ack_reply_caption.wait(),
            timeout=3,
        )
        assert pipeline.model_turn_requests == [CALLER_TEXT, BACKCHANNEL_TEXT], (
            f"Expected model turn requests exactly {[CALLER_TEXT, BACKCHANNEL_TEXT]}, got {pipeline.model_turn_requests}"
        )

        await pipeline.close(close_client=False)

        # Authenticated session API must preserve user utterances
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        user_messages = [m.get("content") for m in messages if m.get("role") == "user"]
        assert user_messages == [CALLER_TEXT, BACKCHANNEL_TEXT, BACKCHANNEL_TEXT], (
            f"Expected exact user messages {[CALLER_TEXT, BACKCHANNEL_TEXT, BACKCHANNEL_TEXT]}, got {user_messages}"
        )
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.parametrize(
    "resume_before_fallback",
    [
        pytest.param(False, id="single-vad"),
        pytest.param(True, id="resumed-before-fallback"),
    ],
)
@pytest.mark.anyio
async def test_isolated_acknowledgment_vad_overlaps_playback_without_stt_until_after_audio_ends(
    monkeypatch,
    resume_before_fallback: bool,
) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="backchannel-during-playback",
    )
    try:
        await pipeline.start(monkeypatch)

        # Wait for initial assistant speech playback to reach output transport
        await asyncio.wait_for(
            pipeline.output.assistant_playback_started.wait(),
            timeout=3,
        )
        assert pipeline.model_turn_requests == [CALLER_TEXT]

        # While assistant audio is actively speaking, deliver external VAD start (no STT yet)
        await pipeline.stt.deliver_vad_start()

        # Observe downstream UserStartedSpeakingFrame as proof workflow processor observed VAD start
        await asyncio.wait_for(
            pipeline.output.user_started_speaking.wait(),
            timeout=3,
        )

        # Playback was not interrupted during active playback
        assert not any(
            isinstance(frame, InterruptionFrame)
            for frame in pipeline.output.recorded_frames
        ), "playback was interrupted by isolated acknowledgment VAD start"

        # Deliver external VAD stop while assistant audio is still playing, arming 400ms fallback
        await pipeline.stt.deliver_vad_stop()

        # Release assistant playback so it drains and completes BEFORE any STT transcript arrives
        pipeline.tts.finish_assistant_playback.set()
        await asyncio.wait_for(
            pipeline.output.assistant_playback_completed.wait(),
            timeout=3,
        )
        await asyncio.wait_for(
            pipeline.connection.assistant_final_caption.wait(),
            timeout=3,
        )

        # For resumed-before-fallback, promptly deliver a second VAD start while previous fallback is pending
        if resume_before_fallback:
            pipeline.output.user_started_speaking.clear()
            await pipeline.stt.deliver_vad_start()
            await asyncio.wait_for(
                pipeline.output.user_started_speaking.wait(),
                timeout=3,
            )
            await pipeline.stt.deliver_vad_stop()

        # Allow the applicable empty fallback timer to elapse
        await asyncio.sleep(0.45)

        # Only now, after assistant audio has fully stopped, deliver the final isolated acknowledgment transcript
        pipeline.connection.ack_final_caption.clear()
        await pipeline.stt.deliver_transcription(BACKCHANNEL_TEXT, speech_final=True)

        # Wait for acknowledgment final caption milestone confirming "okay" was processed
        await asyncio.wait_for(
            pipeline.connection.ack_final_caption.wait(),
            timeout=3,
        )

        # Bounded negative observation window: ensure no model turn was queued
        await asyncio.sleep(0.1)

        # The acknowledgment with VAD overlapping playback must NOT have queued a model turn
        assert BACKCHANNEL_TEXT not in pipeline.model_turn_requests, (
            f"Acknowledgment with VAD overlapping playback incorrectly queued a model turn after audio drained: {pipeline.model_turn_requests}"
        )
        assert pipeline.model_turn_requests == [CALLER_TEXT]

        # Positive control: verify the same acknowledgment spoken as a NEW utterance OUTSIDE playback starts a normal model turn
        pipeline.connection.outside_ack_reply_caption.clear()
        await pipeline.stt.deliver_transcription(BACKCHANNEL_TEXT, speech_final=True)

        await asyncio.wait_for(
            pipeline.connection.outside_ack_reply_caption.wait(),
            timeout=3,
        )
        assert pipeline.model_turn_requests == [CALLER_TEXT, BACKCHANNEL_TEXT], (
            f"Expected model turn requests {[CALLER_TEXT, BACKCHANNEL_TEXT]}, got {pipeline.model_turn_requests}"
        )
        assert pipeline.model_context_turns == [
            [CALLER_TEXT],
            [CALLER_TEXT, BACKCHANNEL_TEXT, BACKCHANNEL_TEXT],
        ], (
            f"Expected context history turns {[[CALLER_TEXT], [CALLER_TEXT, BACKCHANNEL_TEXT, BACKCHANNEL_TEXT]]}, "
            f"got {pipeline.model_context_turns}"
        )

        await pipeline.close(close_client=False)

        # Authenticated session API must preserve user utterances
        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        user_messages = [m.get("content") for m in messages if m.get("role") == "user"]
        assert user_messages == [CALLER_TEXT, BACKCHANNEL_TEXT, BACKCHANNEL_TEXT], (
            f"Expected exact user messages {[CALLER_TEXT, BACKCHANNEL_TEXT, BACKCHANNEL_TEXT]}, got {user_messages}"
        )
    finally:
        await pipeline.close(close_client=True)


HINDI_COMMAND_TEXT = "हाँ, रुकिए"


@pytest.mark.parametrize(
    "ack_text",
    [
        pytest.param("हाँ", id="bare-combining-mark"),
        pytest.param("जी।", id="punctuated-danda"),
    ],
)
@pytest.mark.anyio
async def test_hindi_isolated_acknowledgment_during_playback(
    monkeypatch,
    ack_text: str,
) -> None:
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="backchannel-during-playback",
        language="hi",
        backchannel_text=ack_text,
        command_text=HINDI_COMMAND_TEXT,
    )
    try:
        # 1. Create real hi-configured call/session and start pipeline
        await pipeline.start(monkeypatch)

        # 2. Wait for actual held assistant audio at output.assistant_playback_started
        await asyncio.wait_for(
            pipeline.output.assistant_playback_started.wait(),
            timeout=3,
        )
        assert pipeline.model_turn_requests == [CALLER_TEXT]
        assert pipeline.model_context_turns == [[CALLER_TEXT]]

        # 3. Deliver the case's final Hindi acknowledgment through external FakeDeepgram, speech_final=True
        await pipeline.stt.deliver_transcription(ack_text, speech_final=True)

        # 4. Observe the exact final USER caption for that raw Hindi string
        await asyncio.wait_for(
            pipeline.connection.ack_final_caption.wait(),
            timeout=3,
        )

        # While assistant playback remains held, assert it has not received an InterruptionFrame
        assert not any(
            isinstance(frame, InterruptionFrame)
            for frame in pipeline.output.recorded_frames
        ), f"Hindi acknowledgment {ack_text!r} interrupted held assistant playback"

        # Bounded negative observation window: ensure no model turn was queued
        await asyncio.sleep(0.1)
        assert ack_text not in pipeline.model_turn_requests, (
            f"Isolated Hindi acknowledgment {ack_text!r} queued an unexpected model turn: {pipeline.model_turn_requests}"
        )
        assert pipeline.model_turn_requests == [CALLER_TEXT]

        # 5. Positive control: deliver meaningful Hindi command while playback is still active
        await pipeline.stt.deliver_transcription(HINDI_COMMAND_TEXT, speech_final=True)

        # It must interrupt, produce the fixed command reply, and emit final assistant caption
        await asyncio.wait_for(
            pipeline.connection.command_reply_caption.wait(),
            timeout=3,
        )
        assert any(
            isinstance(frame, InterruptionFrame)
            for frame in pipeline.output.recorded_frames
        ), f"Hindi command {HINDI_COMMAND_TEXT!r} failed to interrupt held playback"
        assert HINDI_COMMAND_TEXT in pipeline.model_turn_requests

        # Release held playback
        pipeline.tts.finish_assistant_playback.set()

        # 6. After command response completion, deliver the SAME case acknowledgment as a NEW utterance outside playback
        pipeline.connection.outside_ack_reply_caption.clear()
        await pipeline.stt.deliver_transcription(ack_text, speech_final=True)

        # It must now get a normal response, not be globally ignored
        await asyncio.wait_for(
            pipeline.connection.outside_ack_reply_caption.wait(),
            timeout=3,
        )

        # 7. Assert exact external model request sequence and context snapshots
        assert pipeline.model_turn_requests == [CALLER_TEXT, HINDI_COMMAND_TEXT, ack_text], (
            f"Expected model turn requests {[CALLER_TEXT, HINDI_COMMAND_TEXT, ack_text]}, "
            f"got {pipeline.model_turn_requests}"
        )
        assert pipeline.model_context_turns == [
            [CALLER_TEXT],
            [CALLER_TEXT, ack_text, HINDI_COMMAND_TEXT],
            [CALLER_TEXT, ack_text, HINDI_COMMAND_TEXT, ack_text],
        ], (
            f"Expected context history turns {[[CALLER_TEXT], [CALLER_TEXT, ack_text, HINDI_COMMAND_TEXT], [CALLER_TEXT, ack_text, HINDI_COMMAND_TEXT, ack_text]]}, "
            f"got {pipeline.model_context_turns}"
        )

        # 8. Close/flush pipeline before authenticated API reads, retaining the client
        await pipeline.close(close_client=False)

        response = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        user_messages = [m.get("content") for m in messages if m.get("role") == "user"]
        assert user_messages == [CALLER_TEXT, ack_text, HINDI_COMMAND_TEXT, ack_text], (
            f"Expected exact user messages {[CALLER_TEXT, ack_text, HINDI_COMMAND_TEXT, ack_text]}, got {user_messages}"
        )
        assistant_messages = [m.get("content") for m in messages if m.get("role") == "assistant"]
        assert assistant_messages == [COMMAND_REPLY_TEXT, OUTSIDE_ACK_REPLY_TEXT], (
            f"Expected assistant messages {[COMMAND_REPLY_TEXT, OUTSIDE_ACK_REPLY_TEXT]}, got {assistant_messages}"
        )
    finally:
        await pipeline.close(close_client=True)
