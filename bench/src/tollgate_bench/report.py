"""Turn the JSON lines a benchmark run prints into the Markdown table in bench/RESULTS.md."""

import json
from collections import defaultdict
from collections.abc import Iterable


def read_records(lines: Iterable[str]) -> list[dict]:
    """JSON records from a Job log. Other lines (uv's download messages, progress) are skipped."""
    records = []
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "–"
    if value < 1:
        return f"{value * 1000:.0f} ms"
    return f"{value:.1f} s"


def fmt_number(value: float | None, decimals: int = 1, suffix: str = "") -> str:
    return "–" if value is None else f"{value:.{decimals}f}{suffix}"


def markdown_table(records: list[dict]) -> str:
    levels = [r for r in records if r.get("type") == "level"]
    by_backend: dict[str, list[dict]] = defaultdict(list)
    for level in levels:
        by_backend[level["backend"]].append(level)
    out = []
    for backend, rows in by_backend.items():
        rows.sort(key=lambda r: r["concurrency"])
        prompt = [r["mean_prompt_tokens"] for r in rows if r["mean_prompt_tokens"]]
        output = [r["mean_output_tokens"] for r in rows if r["mean_output_tokens"]]
        tokens = (
            f"{sum(prompt) / len(prompt):.0f} prompt / {sum(output) / len(output):.0f} output "
            "tokens per request"
            if prompt and output
            else "token counts unavailable"
        )
        out += [
            f"### {backend}",
            "",
            tokens,
            "",
            "| Users | OK / sent | Output tok/s | Req/s | TTFT p50 | TTFT p95 | Time per token p50 "
            "| Time per token p95 | GPU max | GPU min clock |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in rows:
            out.append(
                f"| {r['concurrency']} | {r['requests'] - r['errors']} / {r['requests']} "
                f"| {fmt_number(r['output_tokens_per_s'])} | {fmt_number(r['requests_per_s'], 2)} "
                f"| {fmt_seconds(r['ttft_p50_s'])} | {fmt_seconds(r['ttft_p95_s'])} "
                f"| {fmt_seconds(r['tpot_p50_s'])} | {fmt_seconds(r['tpot_p95_s'])} "
                f"| {fmt_number(r.get('gpu_temp_max_c'), 0, ' °C')} "
                f"| {fmt_number(r.get('gpu_clock_min_mhz'), 0, ' MHz')} |"
            )
        out.append("")
    return "\n".join(out)
