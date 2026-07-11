from omnidesk_agent.appsync.conversation_context import ConversationContextBuilder


def test_context_builder_includes_prior_turns_and_excludes_current() -> None:
    context = ConversationContextBuilder(max_messages=4).build(
        [
            {"message_id": "1", "role": "user", "content": "My name is Ada"},
            {"message_id": "2", "role": "assistant", "content": "Hello Ada"},
            {"message_id": "3", "role": "user", "content": "What is my name?"},
        ],
        current_message_id="3",
    )

    assert "User: My name is Ada" in context
    assert "Assistant: Hello Ada" in context
    assert "What is my name?" not in context


def test_context_builder_redacts_common_secret_shapes_and_bounds_history() -> None:
    context = ConversationContextBuilder(max_messages=1, max_characters=200).build(
        [
            {"message_id": "1", "role": "user", "content": "old"},
            {
                "message_id": "2",
                "role": "assistant",
                "content": "Authorization: Bearer secret-value api_key=abc123",
            },
            {"message_id": "3", "role": "user", "content": "current"},
        ],
        current_message_id="3",
    )

    assert "old" not in context
    assert "secret-value" not in context
    assert "abc123" not in context
    assert context.count("[REDACTED]") == 2
