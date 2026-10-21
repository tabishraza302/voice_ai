"""The history the model sees must be well-formed.

The bug these cover is in the traces, not in a reproduction script: a
barge-in left two user messages in a row, and the model answered the
resulting malformed history with "Hello, what is what? Please finish
your question."
"""

from bot.context import merge_consecutive_user_messages


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
