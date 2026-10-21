"""The shape of the conversation history the model sees.

Two user messages in a row is not a conversation. It is a malformed
history, and a model reads it as a fragmented utterance rather than as
something the user said.

It happens when the user speaks again before the bot has finished
answering. Pipecat cancels the in-flight generation, so the assistant
message never lands, and the next user message is appended after the
one before it.

This is not a guess. The traces in ``data/phoenix`` show it: a turn
whose LLM span ended with **no output and no error** — a cancelled
generation, not a failed one — immediately followed by a turn whose
context was ``[user "Hello.", user "Can you hear me?"]``, and later a
turn whose context was the same sentence three times over. The model's
answer to that third turn was "Hello, what is what? Please finish your
question."

So the fix belongs here, where the history is written: consecutive user
messages are merged into the single turn they really are, before the
model ever sees them.
"""

from __future__ import annotations

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def _text_of(message: LLMContextMessage) -> str | None:
    """The message's text, or ``None`` if it is not plain text.

    A message can also carry structured content — an image, a file, a
    list of content parts. Those are not merged: concatenating them
    would corrupt them, so they are left exactly as they are.
    """
    content = message.get("content")
    return content if isinstance(content, str) else None


def merge_consecutive_user_messages(
    messages: list[LLMContextMessage],
) -> list[LLMContextMessage]:
    """Merge adjacent user messages into one turn.

    Two user messages in a row are one utterance the user said in two
    pieces — or one utterance the user repeated. Either way the model
    should see a single turn. Empty text is dropped, so an empty
    transcription between two real ones leaves no trace.

    Messages that are not plain text are left untouched, and every other
    message keeps its position, its role and its content.
    """
    merged: list[LLMContextMessage] = []

    for message in messages:
        previous = merged[-1] if merged else None
        if message.get("role") == "user" and previous is not None and previous.get("role") == "user":
            previous_text = _text_of(previous)
            text = _text_of(message)
            if previous_text is None or text is None:
                # Structured content: leave it alone rather than corrupt it.
                merged.append(message)
                continue
            joined = " ".join(part for part in (previous_text.strip(), text.strip()) if part)
            merged[-1] = {**previous, "content": joined}
            continue
        merged.append(message)

    return merged


class ContextShapingProcessor(FrameProcessor):
    """Keeps the history the model receives well-formed.

    This sits between the user aggregator and the LLM, so it sees every
    context frame on its way to the model and can rewrite the history
    in place. The rewrite is applied to the shared context, so the
    assistant's reply is appended to the merged turn and the stored
    history is well-formed too.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, LLMContextFrame):
            frame.context.transform_messages(merge_consecutive_user_messages)
        await self.push_frame(frame, direction)
