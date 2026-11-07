from tollgate_bench.sse import has_content, parse_line


def test_data_line_is_parsed() -> None:
    assert parse_line('data: {"choices": []}') == {"choices": []}


def test_done_blank_and_comment_lines_are_ignored() -> None:
    assert parse_line("data: [DONE]") is None
    assert parse_line("") is None
    assert parse_line(": keep-alive") is None


def test_content_chunk_is_detected() -> None:
    assert has_content({"choices": [{"delta": {"content": "Hi"}}]})


def test_role_only_and_usage_chunks_have_no_content() -> None:
    assert not has_content({"choices": [{"delta": {"role": "assistant"}}]})
    assert not has_content({"choices": [], "usage": {"completion_tokens": 3}})
    assert not has_content({"choices": [{"delta": {"content": ""}}]})
