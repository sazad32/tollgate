"""Command line: `python -m tollgate_bench run|job|report|fairness ...` (see bench/README.md)."""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict

import httpx

from tollgate_bench.client import run_level
from tollgate_bench.fairness import fairness_table, run_fairness, summarize_fairness
from tollgate_bench.gpu import gpu_window
from tollgate_bench.job import manifest
from tollgate_bench.report import markdown_table, read_records
from tollgate_bench.stats import summarize
from tollgate_bench.targets import TARGETS

WARMUP_REQUESTS = 2


def emit(record: dict) -> None:
    """One JSON object per line on stdout; that's the raw result. Progress goes to stderr."""
    print(json.dumps(record), flush=True)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


async def run(args: argparse.Namespace) -> None:
    levels = [int(x) for x in args.levels.split(",")]
    counts = [int(x) for x in args.requests.split(",")]
    if len(levels) != len(counts):
        raise SystemExit("--levels and --requests need the same number of values")
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    gpu_nodes = args.gpu_nodes.split(",") if args.gpu_nodes else []
    emit({"type": "run", "backend": args.backend, "url": args.url, "model": args.model,
          "run_id": args.run_id, "started_at": time.time()})  # fmt: skip

    # Warm-up: make sure the model is loaded and kernels are compiled before anything is timed.
    log(f"warm-up: {WARMUP_REQUESTS} requests")
    warm = await run_level(args.url, args.model, 1, WARMUP_REQUESTS,
                           run_id=f"{args.run_id}-warmup", api_key=api_key)  # fmt: skip
    if any(r.error for r in warm.results):
        raise SystemExit(f"warm-up failed: {warm.results[0].error}")

    async with httpx.AsyncClient(timeout=10.0) as prometheus:
        for concurrency, total in zip(levels, counts, strict=True):
            log(f"level {concurrency} users: {total} requests")
            level = await run_level(args.url, args.model, concurrency, total,
                                    run_id=args.run_id, api_key=api_key)  # fmt: skip
            for result in level.results:
                emit({"type": "request", "backend": args.backend, **asdict(result)})
            summary = asdict(summarize(args.backend, level))
            gpu = {}
            if gpu_nodes and args.prometheus_url:
                gpu = await gpu_window(prometheus, args.prometheus_url, gpu_nodes,
                                       level.window_start, level.window_end)  # fmt: skip
            emit({"type": "level", **summary, **gpu,
                  "window_start": level.window_start, "window_end": level.window_end})  # fmt: skip
            log(
                f"level {concurrency}: {summary['output_tokens_per_s']:.1f} tok/s, "
                f"{summary['errors']} errors, {summary['duration_s']:.0f} s"
            )
    emit({"type": "done", "backend": args.backend, "finished_at": time.time()})


async def fairness(args: argparse.Namespace) -> None:
    flood_key, trickle_key = os.environ[args.flood_key_env], os.environ[args.trickle_key_env]
    log(f"{args.label}: a burst of {args.flood}, then {args.trickle} more every "
        f"{args.trickle_every:.0f} s from the other tenant")  # fmt: skip
    results = await run_fairness(
        args.url, args.model, flood_key, trickle_key, run_id=args.run_id, flood=args.flood,
        trickle=args.trickle, trickle_start_s=args.trickle_start,
        trickle_every_s=args.trickle_every, max_tokens=args.max_tokens,
        trickle_url=args.trickle_url,
    )  # fmt: skip
    for role, result in results:
        emit({"type": "fairness_request", "label": args.label, "tenant": role, **asdict(result)})
    summaries = summarize_fairness(args.label, results)
    for summary in summaries:
        emit(summary)
    log("summary:\n" + fairness_table(summaries))


def main(argv: list[str] | None = None) -> None:
    # Windows writes redirected output in its legacy code page, which garbles "°C" in RESULTS.md.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="tollgate_bench")
    commands = parser.add_subparsers(dest="command", required=True)

    p_run = commands.add_parser("run", help="run the benchmark (normally inside the cluster Job)")
    p_run.add_argument("--backend", required=True, help="label for the results")
    p_run.add_argument("--url", required=True, help="server base URL, without /v1")
    p_run.add_argument("--model", required=True)
    p_run.add_argument("--levels", default="1,8,32,64", help="concurrent users per level")
    p_run.add_argument("--requests", default="10,32,64,128", help="requests per level")
    p_run.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    p_run.add_argument("--api-key-env", help="environment variable holding the API key")
    p_run.add_argument("--gpu-nodes", help="comma-separated node names to record GPU data for")
    p_run.add_argument("--prometheus-url")

    p_job = commands.add_parser("job", help="print the Kubernetes Job for a target, as JSON")
    p_job.add_argument("target", choices=sorted(TARGETS))
    p_job.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    p_job.add_argument("--levels", help="override the target's levels, e.g. 1,2")
    p_job.add_argument("--requests", help="override the target's requests per level")
    p_job.add_argument("--label", help="override the backend label in the results")

    p_report = commands.add_parser("report", help="Markdown results table from Job logs")
    p_report.add_argument("files", nargs="+")

    p_fair = commands.add_parser("fairness", help="two-tenant fairness demo against the gateway")
    p_fair.add_argument("--url", required=True, help="gateway base URL, without /v1")
    p_fair.add_argument("--trickle-url", help="another replica for the trickle (default: --url)")
    p_fair.add_argument("--model", default="smollm3-3b")
    p_fair.add_argument("--label", required=True, help="name of this run in the table")
    p_fair.add_argument("--flood-key-env", required=True, help="env var with the burst's API key")
    p_fair.add_argument("--trickle-key-env", required=True, help="env var: the other tenant's key")
    p_fair.add_argument("--flood", type=int, default=12, help="requests in the burst")
    p_fair.add_argument("--trickle", type=int, default=5, help="requests from the other tenant")
    p_fair.add_argument("--trickle-start", type=float, default=2.0, help="seconds after the burst")
    p_fair.add_argument("--trickle-every", type=float, default=5.0, help="seconds between them")
    p_fair.add_argument("--max-tokens", type=int, default=128)
    p_fair.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))

    args = parser.parse_args(argv)
    if args.command == "run":
        asyncio.run(run(args))
    elif args.command == "fairness":
        asyncio.run(fairness(args))
    elif args.command == "job":
        levels = tuple(int(x) for x in args.levels.split(",")) if args.levels else None
        counts = tuple(int(x) for x in args.requests.split(",")) if args.requests else None
        job = manifest(TARGETS[args.target], args.run_id, levels, counts, args.label)
        print(json.dumps(job, indent=2))
    else:
        records = []
        for name in args.files:
            with open(name, encoding="utf-8") as f:
                records += read_records(f)
        print(markdown_table(records))
        print(fairness_table(records))


if __name__ == "__main__":
    main()
