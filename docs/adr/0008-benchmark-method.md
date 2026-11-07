# 0008 — Benchmark method: our own async client, run inside the cluster

- Status: accepted
- Date: 2026-11-07

## Context
The Phase 1 gate is a published table of time to first token (TTFT) and tokens/s at 1, 8, 32 and 64
concurrent users on each backend: `llama-server` on node-1 and node-2, and vLLM on the workstation.
The numbers have to be comparable across very different hardware, repeatable, and defensible line by
line. TTFT needs streamed responses timed chunk by chunk.

## Options considered
1. **k6:** no new language runtime for us to learn, but it can't time the chunks of a streamed response,
   so it can't measure TTFT.
2. **Locust:** Python, but a new dependency, built on gevent rather than asyncio, and awkward with
   streaming.
3. **`vllm bench serve`:** the industry-standard tool, but it ships in a 23 GB image and is a black box
   we couldn't defend line by line.
4. **Our own client:** about 300 lines of asyncio + httpx (both in the stack), with tests.

Where the client runs: on the workstation through `kubectl port-forward` (a single tunnel that can drop
under load and adds a hop the gateway won't have), or inside the cluster as a Job.

## Decision
Option 4, run as a Kubernetes Job (`bench/`, `python -m tollgate_bench`):
- **Closed loop:** N users, each sending its next request as soon as the last one ends. Requests per
  level: 10, 32, 64, 128, so every user sends at least two. Two warm-up requests first, not counted.
- **Fixed workload:** a synthetic support ticket (about 280 prompt tokens with the chat template) and
  exactly 128 output tokens, forced with `ignore_eos`. Each ticket starts with a unique ID, so only the
  chat template's fixed opening can be served from a prompt cache.
- **Server-counted tokens:** token counts come from the server's usage report, not a client tokenizer.
- **Placement:** the Job runs on the node that isn't being measured, so it doesn't share CPU with the
  server, and its traffic takes the same network path the gateway's will. vLLM is reached through the
  `vllm-workstation` Service (ADR 0007).
- **GPU state:** each level records the hottest GPU temperature and lowest core clock from Prometheus,
  so a slow level can be told apart from a throttling GPU.
- **No image to build:** the code is mounted from a ConfigMap and run with uv's Python image, pinned by
  digest.

## Consequences
- The numbers measure the engines as the gateway will see them, without any gateway overhead. Phase 4
  re-runs the same client through the gateway to measure that overhead.
- Closed loop means users wait for each other: at high concurrency, TTFT is mostly queueing time. That
  matches users of a shared support tool, not open internet traffic, where requests keep arriving
  regardless.
- One run per level, so run-to-run variance isn't measured. The original week 1 note asked for three
  runs; at about 20 minutes per node, that's deferred until a result needs it.
- The client counts a request that fails before any response as an error and leaves it out of the
  timings. Its default keep-alive time (5 s) equals llama-server's, which caused a few such failures
  (see `bench/RESULTS.md`); the gateway must not inherit that.
- Results depend on the client being light. It used about 5 millicores and 40 MiB per run.
