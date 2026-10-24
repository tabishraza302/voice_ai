"""The history the model sees must be well-formed.

The bug these cover is in the traces, not in a reproduction script: a
barge-in left two user messages in a row, and the model answered the
resulting malformed history with "Hello, what is what? Please finish
your question."
"""

from bot.context import ContextShapingProcessor, merge_consecutive_user_messages


def test_two_user_messages_become_one_turn():
    messages = [
        {"role": "user", "content": "Hello."},
        {"role": "user", "content": "Can you hear me?"},
    ]
    assert merge_consecutive_user_messages(messages) == [
        {"role": "user", "content": "Hello. Can you hear me?"}
    ]


def test_a_repeated_utterance_is_one_turn():
    messages = [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "user", "content": "What is the capital of France?"},
    ]
    merged = merge_consecutive_user_messages(messages)
    assert len(merged) == 1
    assert merged[0]["content"] == (
        "What is the capital of France? What is the capital of France? "
        "What is the capital of France?"
    )


def test_a_well_formed_history_is_untouched():
    messages = [
        {"role": "system", "content": "You are a voice assistant."},
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "Hi, how can I help?"},
        {"role": "user", "content": "How are you?"},
    ]
    assert merge_consecutive_user_messages(messages) == messages


def test_an_empty_transcript_leaves_no_empty_turn():
    messages = [
        {"role": "user", "content": "Hello."},
        {"role": "user", "content": ""},
    ]
    assert merge_consecutive_user_messages(messages) == [{"role": "user", "content": "Hello."}]


def test_structured_content_is_not_merged():
    """Structured content is left alone: concatenating it would corrupt it."""
    image = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
    messages = [
        image,
        {"role": "user", "content": "What is this?"},
    ]
    merged = merge_consecutive_user_messages(messages)
    assert merged[0] == image
    assert len(merged) == 2


def test_the_merged_history_keeps_its_place():
    messages = [
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "Hi."},
        {"role": "user", "content": "One."},
        {"role": "user", "content": "Two."},
        {"role": "assistant", "content": "Three."},
    ]
    merged = merge_consecutive_user_messages(messages)
    assert [m["role"] for m in merged] == ["user", "assistant", "user", "assistant"]
    assert merged[2]["content"] == "One. Two."


class TestTheFrameReachesTheModel:
    """The shaper must not swallow the frame the model runs on.

    A processor starts its frame-processing task when the base class sees
    the StartFrame, so a ``process_frame`` that overrides the base one
    without calling ``super()`` never gets that task — and every frame
    that is not a system frame sits in its queue and is never processed.
    The context frame is not a system frame, so the model would never see
    it, and the bot would never answer.
    """

    async def test_the_context_frame_is_passed_on_and_merged(self):
        import asyncio
        import contextlib

        from pipecat.frames.frames import LLMContextFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
        from pipecat.workers.runner import WorkerRunner

        class Capture(FrameProcessor):
            """Stands in for the LLM service."""

            def __init__(self) -> None:
                super().__init__()
                self.contexts: list[LLMContext] = []

            async def process_frame(self, frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                if isinstance(frame, LLMContextFrame):
                    self.contexts.append(frame.context)
                await self.push_frame(frame, direction)

        capture = Capture()
        pipeline = Pipeline([ContextShapingProcessor(), capture])
        worker = PipelineWorker(pipeline, params=PipelineParams())
        runner = WorkerRunner()
        await runner.add_workers(worker)
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.5)

        context = LLMContext(
            messages=[
                {"role": "user", "content": "Hello."},
                {"role": "user", "content": "Can you hear me?"},
            ]
        )
        await worker.queue_frame(LLMContextFrame(context=context))
        await asyncio.sleep(0.5)

        # The pipeline is taken down rather than ended: a processor that
        # drops the frame would drop the EndFrame too, and the run would
        # never finish.
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

        assert len(capture.contexts) == 1, "the context frame never reached the model"
        assert [m["content"] for m in capture.contexts[0].messages] == [
            "Hello. Can you hear me?"
        ]
