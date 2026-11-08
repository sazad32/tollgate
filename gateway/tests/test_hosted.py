"""The hosted model adapter: OpenAI chat format in, Claude Messages API underneath, OpenAI out.

The Anthropic SDK runs for real here, against a fake Anthropic API (conftest.FakeAnthropic), so
the translation is tested against the events and objects the SDK actually produces.
"""

import json

import anthropic
import httpx2
import pytest
import pytest_asyncio
from tollgate_gateway.hosted import (
    DataPolicyError,
    HostedModel,
    finish_reason,
    unsupported_reason,
)
from tollgate_gateway.tenants import Tenant

pytestmark = pytest.mark.asyncio

ALLOWED = Tenant(id="engineering", name="Engineering", data_policy="hosted_allowed")
LOCAL_ONLY = Tenant(id="support", name="Support")


@pytest_asyncio.fixture
async def hosted(claude):
    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(claude.handle)
        ),
        max_retries=0,
    )
    yield HostedModel(
        client,
        model="claude-opus-5-5",
        effort="low",
        default_max_tokens=4096,
        refusal_fallback=True,
    )
    await client.close()


def body(*messages: dict, **extra: object) -> dict:
    return {"model": "claude-opus-5-5", "messages": list(messages), **extra}


USER = {"role": "user", "content": "Hi"}


# --- the request ---------------------------------------------------------------------------------


async def test_system_messages_become_the_system_prompt(hosted) -> None:
    params = hosted.params(body({"role": "system", "content": "Be brief."}, USER))

    assert params["system"] == "Be brief."
    assert params["messages"] == [{"role": "user", "content": "Hi"}]


async def test_developer_messages_count_as_system_too(hosted) -> None:
    params = hosted.params(body({"role": "developer", "content": "A"}, USER))

    assert params["system"] == "A"


async def test_text_parts_are_joined(hosted) -> None:
    parts = [{"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]

    params = hosted.params(body({"role": "user", "content": parts}))

    assert params["messages"] == [{"role": "user", "content": "Hello there"}]


async def test_the_configured_model_and_effort_are_used(hosted) -> None:
    params = hosted.params(body(USER))

    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"] == {"effort": "low"}


async def test_refusal_fallback_is_on(hosted) -> None:
    params = hosted.params(body(USER))

    assert params["fallbacks"] == "default"
    assert params["betas"] == ["server-side-fallback-2026-07-01"]


async def test_refusal_fallback_can_be_turned_off(hosted) -> None:
    hosted.refusal_fallback = False

    params = hosted.params(body(USER))

    assert "fallbacks" not in params and "betas" not in params


async def test_max_completion_tokens_wins_then_max_tokens_then_the_default(hosted) -> None:
    assert hosted.params(body(USER, max_completion_tokens=50, max_tokens=99))["max_tokens"] == 50
    assert hosted.params(body(USER, max_tokens=99))["max_tokens"] == 99
    assert hosted.params(body(USER))["max_tokens"] == 4096


async def test_stop_becomes_stop_sequences(hosted) -> None:
    assert hosted.params(body(USER, stop="END"))["stop_sequences"] == ["END"]
    assert hosted.params(body(USER, stop=["A", "B"]))["stop_sequences"] == ["A", "B"]


async def test_sampling_options_claude_rejects_are_dropped(hosted) -> None:
    params = hosted.params(body(USER, temperature=0.2, top_p=0.9, seed=7, presence_penalty=1))

    assert not {"temperature", "top_p", "seed", "presence_penalty"} & params.keys()


# --- what the hosted model can't take ------------------------------------------------------------


@pytest.mark.parametrize(
    "request_body",
    [
        body(USER, tools=[{"type": "function", "function": {"name": "f"}}]),
        body(USER, response_format={"type": "json_object"}),
        body(USER, n=2),
        body({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}),
        body(USER, {"role": "tool", "content": "42", "tool_call_id": "c1"}),
        body(USER, {"role": "assistant", "content": "Sure,"}),  # ends on the assistant: prefill
        body({"role": "system", "content": "Only a system prompt."}),
        body({"role": "user", "content": 42}),
    ],
)
async def test_requests_the_translation_cannot_carry_are_flagged(request_body) -> None:
    assert unsupported_reason(request_body) is not None


async def test_a_plain_chat_is_supported() -> None:
    chat = body(
        {"role": "system", "content": "S"},
        USER,
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "And?"},
        response_format={"type": "text"},
        n=1,
    )

    assert unsupported_reason(chat) is None


# --- the reply -----------------------------------------------------------------------------------


async def test_a_reply_becomes_an_openai_completion(hosted, claude) -> None:
    reply = await hosted.complete(ALLOWED, body(USER), request_id="r1")

    assert reply["object"] == "chat.completion"
    assert reply["id"] == "chatcmpl-r1"
    assert reply["model"] == "claude-opus-5-5"
    assert reply["choices"] == [
        {"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}
    ]
    assert reply["usage"] == {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}


async def test_the_request_reaches_anthropic_translated(hosted, claude) -> None:
    await hosted.complete(ALLOWED, body({"role": "system", "content": "S"}, USER), request_id="r")

    sent = claude.last_body
    assert sent["system"] == "S"
    assert sent["messages"] == [{"role": "user", "content": "Hi"}]
    assert claude.requests[-1].headers["anthropic-beta"] == "server-side-fallback-2026-07-01"


async def test_usage_counts_every_attempt_when_a_refusal_fell_back(hosted, claude) -> None:
    # The first model declined after 4 tokens, the fallback model answered: both are billed.
    claude.iterations = [
        {"type": "message", "input_tokens": 12, "output_tokens": 4},
        {"type": "fallback_message", "input_tokens": 12, "output_tokens": 7},
    ]

    reply = await hosted.complete(ALLOWED, body(USER), request_id="r")

    assert reply["usage"] == {"prompt_tokens": 24, "completion_tokens": 11, "total_tokens": 35}


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
        (None, "stop"),
    ],
)
async def test_finish_reasons_are_mapped(stop_reason, expected) -> None:
    assert finish_reason(stop_reason) == expected


# --- streams -------------------------------------------------------------------------------------


def events(raw: bytes) -> list:
    out = []
    for block in raw.decode().split("\n\n"):
        if block.startswith("data: "):
            data = block[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


async def test_a_stream_becomes_openai_chunks(hosted) -> None:
    stream = await hosted.stream(ALLOWED, body(USER, stream=True), request_id="r2")
    raw = b"".join([chunk async for chunk in stream.chunks()])
    await stream.close()

    sent = events(raw)
    assert all(e["id"] == "chatcmpl-r2" for e in sent[:-1])
    assert all(e["object"] == "chat.completion.chunk" for e in sent[:-1])
    assert [e["choices"][0]["delta"] for e in sent[:4]] == [
        {"role": "assistant", "content": ""},
        {"content": "Hel"},
        {"content": "lo"},
        {},
    ]
    assert sent[3]["choices"][0]["finish_reason"] == "stop"
    assert sent[4]["choices"] == []
    assert sent[4]["usage"] == {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
    assert sent[5] == "[DONE]"


async def test_a_stream_that_breaks_ends_with_an_error_event(hosted, claude) -> None:
    claude.break_stream = True

    stream = await hosted.stream(ALLOWED, body(USER, stream=True), request_id="r")
    raw = b"".join([chunk async for chunk in stream.chunks()])
    await stream.close()

    last = events(raw)[-1]
    assert last["error"]["code"] == "stream_interrupted"


# --- the data-policy guard -----------------------------------------------------------------------


async def test_a_local_only_tenant_can_never_reach_anthropic(hosted, claude) -> None:
    # The router already refuses these. This second check sits right where data would leave.
    with pytest.raises(DataPolicyError):
        await hosted.complete(LOCAL_ONLY, body(USER), request_id="r")
    with pytest.raises(DataPolicyError):
        await hosted.stream(LOCAL_ONLY, body(USER, stream=True), request_id="r")

    assert claude.requests == []
