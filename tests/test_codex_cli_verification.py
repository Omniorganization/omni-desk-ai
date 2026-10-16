import json

import httpx
import pytest

from scripts.verify_codex_cli_chat import checked_stream


def response(answer, *, provider="codex_cli", model="gpt-6.1-sol"):
    events = [("chat.started", {}), ("chat.reasoning.delta", {"text": "NO_TOOLS"}),
              ("chat.delta", {"text": answer, "provider": provider, "model": model, "native": False}),
              ("chat.completed", {})]
    return httpx.Response(200, text="".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events))


@pytest.mark.parametrize("answer,provider,model", [
    ("NO_TOOLS is just a marker", "codex_cli", "gpt-6.1-sol"),
    ("wrong answer", "codex_cli", "gpt-6.1-sol"),
    ("NO_TOOLS", "fixture", "gpt-6.1-sol"),
    ("NO_TOOLS", "codex_cli", "other-model"),
])
def test_live_verification_rejects_wrong_assistant_answer_or_route(answer, provider, model):
    with pytest.raises(AssertionError):
        checked_stream(response(answer, provider=provider, model=model), "NO_TOOLS", "gpt-6.1-sol")


def test_live_verification_reports_actual_assistant_text_and_buffered_stream():
    result = checked_stream(response("NO_TOOLS"), "NO_TOOLS", "gpt-6.1-sol")
    assert result["answer"] == "NO_TOOLS" and result["native_stream"] is False
