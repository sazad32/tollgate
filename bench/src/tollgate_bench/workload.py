"""The fixed workload every backend gets (ADR 0008).

Each request: a synthetic support ticket of about 256 prompt tokens (with the chat template) and
exactly 128 output tokens. `ignore_eos` makes the model keep going past its natural end, so every
backend generates the same number of tokens per request.

The ticket text starts with a unique ID, so a backend can't reuse the computed prompt of an earlier
request. Only the chat template's fixed opening (about 60 tokens, before the user message) can be
cached, as it would be for any deployment with a fixed system prompt.
"""

MAX_TOKENS = 128

# Synthetic text only (CLAUDE.md rule 2). About 190 tokens with the SmolLM3 tokenizer.
_TICKET = (
    "Customer on the Prepaid Plus plan writes: my March invoice shows three roaming charges "
    "from a trip to Portugal, but I bought the Europe travel pass before leaving and the app "
    "confirmed it was active. The charges are for mobile data on the 4th, 5th and 6th, about "
    "41 dollars in total. I also see a late fee of 7 dollars, even though autopay ran on the "
    "due date and my bank shows the payment cleared the next morning. Last month support told "
    "me the pass would cover all data in the EU, so I don't understand why I was billed. I have "
    "been with you for six years and this is the second billing mistake this year. Please "
    "refund the roaming charges and the late fee, confirm the travel pass is still on my "
    "account for my trip in June, and tell me whether I need to do anything differently next "
    "time so this doesn't happen again."
)


def ticket(request_id: str) -> str:
    """Return the prompt text for one request, starting with its unique ID."""
    return (
        f"Ticket {request_id}. {_TICKET}\n\nSummarise the issue and draft a reply to the customer."
    )


def chat_body(model: str, request_id: str, max_tokens: int = MAX_TOKENS) -> dict:
    """Return the OpenAI-compatible request body for one streamed chat completion."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": ticket(request_id)}],
        "max_tokens": max_tokens,
        "ignore_eos": True,
        "temperature": 0,
        "stream": True,
        # The final chunk then carries token counts, so we use the server's count, not ours.
        "stream_options": {"include_usage": True},
    }
