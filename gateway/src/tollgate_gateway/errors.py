"""Error responses in the OpenAI format, so existing OpenAI client libraries can read them.

{"error": {"message": "...", "type": "...", "code": "..."}}
"""

import json

import httpx
from fastapi.responses import JSONResponse


def openai_error(status: int, message: str, type_: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": type_, "code": code}}
    )


def upstream_failure(exc: httpx.HTTPError) -> JSONResponse:
    """The response for a model server we couldn't get an answer from."""
    if isinstance(exc, httpx.TimeoutException):
        return openai_error(
            504, "The model server did not respond in time.", "upstream_error", "upstream_timeout"
        )
    if isinstance(exc, httpx.ConnectError | httpx.RemoteProtocolError):
        return openai_error(
            503, "The model server is unavailable.", "upstream_error", "upstream_unavailable"
        )
    return openai_error(502, "The model server failed.", "upstream_error", "upstream_failed")


def sse_error_event(message: str) -> bytes:
    """An error sent inside a stream, after the 200 status has already gone out."""
    payload = {
        "error": {"message": message, "type": "upstream_error", "code": "stream_interrupted"}
    }
    return f"data: {json.dumps(payload)}\n\n".encode()
