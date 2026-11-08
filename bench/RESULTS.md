# Benchmark results: Phase 1

Model servers measured on their own, without the gateway, on 2026-11-07 (run `w4-20261107-1249`).
Method: [ADR 0008](../docs/adr/0008-benchmark-method.md). How to re-run: [README](README.md).
Two tenants sharing the GPUs through the gateway (week 9): [at the end](#two-tenants-one-set-of-gpus-week-9-phase-2-gate).

## Headline
Same model (SmolLM3-3B, 4-bit), same requests (281 prompt tokens, 128 output tokens), closed loop.

**Output tokens per second, all users together**

| Users | node-2 (GTX 960M) | node-1 (GTX 860M) | both nodes (Service) | vLLM (RTX 3060 Laptop) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 11.7 | 10.0 | 10.9 | 113 |
| 8 | 20.2 | 17.1 | 35.6 | 613 |
| 32 | 20.7 | 17.2 | 29.0 | 1,088 |
| 64 | 20.7 | 17.3 | 34.5 | 1,103 |

**Time to first token, median (p50)**

| Users | node-2 | node-1 | both nodes | vLLM |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 1.8 s | 2.1 s | 2.0 s | 77 ms |
| 8 | 40 s | 47 s | 15 s | 454 ms |
| 32 | 188 s | 224 s | 65 s | 1.1 s |
| 64 | 386 s | 460 s | 170 s | 4.5 s |

936 requests in total; 6 failed (0.64%), all for the same cause (see *Errors*).

## What the numbers say
1. **A node serves two users; everyone else waits.** Each `llama-server` has 2 slots (2 × 8k context,
   ADR 0005). Throughput is flat from 8 users on, and extra users only add queueing: at 8 users the
   median wait for a first token is 40–47 s. For interactive use, a node is good for about 2 users.
2. **Batching is nearly free on these GPUs.** Two slots run at almost the same time per token as one
   (72 → 74 ms on node-2), so throughput nearly doubles. Generation is limited by memory bandwidth, not
   compute. The 2-slot limit is a VRAM choice: the same 16k-token KV budget as 4 slots × 4k context could
   roughly double a node's throughput again, at the cost of shorter requests (ADR 0005 consequence).
3. **vLLM is in a different class.** At 1 user it's 10× a node; at full load, 53–64× one node and
   31× both. It levels off from 32 users because the compose file caps it at 32 requests at once
   (`--max-num-seqs=32`); its KV cache (18,512 tokens) would fit about 45 of these requests. Even at
   64 users, p95 time to first token stays under 5.5 s.
4. **The Kubernetes Service spreads connections, not load.** Both nodes behind the `llama-server` Service
   reach about the sum of the two (35.6 of 37.3 tok/s at 8 users), but at 32 and 64 users throughput
   dips, and p95 waits run 1.5–2.3× the median. kube-proxy assigns each *connection* to a pod at random, so one node's
   queue can run long while the other's is short or empty. The gateway (weeks 8–9) should route by
   each backend's queue (`llamacpp:requests_processing` / `requests_deferred`) instead of relying on
   the Service.
5. **No thermal throttling.** In the single-node runs the core clock never dropped below its maximum
   (1,202 MHz on node-2, 1,097 MHz on node-1), although node-1 reached 86 °C. The both-nodes row's
   135 MHz minimum is a GPU sitting idle (one user, or the other node's queue had drained), not
   throttling.

**For the router (week 8):** send work to vLLM whenever the workstation is on, and use the nodes as the
always-on fallback for light load. A node can't absorb a burst: queue at the gateway and answer
`429` with a retry hint, rather than letting requests wait minutes inside `llama-server`.

## Errors
All 6 failures are `Server disconnected without sending a response` after 0.0 s, on `llama-server` only.
The client reused a pooled keep-alive connection at the moment the server closed it: cpp-httplib (inside
`llama-server`) closes idle connections after 5 s, and httpx keeps them for 5 s by default. The failed
requests never started, so they don't affect the timings; they're counted as errors and left out.
**For the gateway (week 5):** keep the upstream client's keep-alive expiry below 5 s, and retry once when
a connection closes before any response byte arrives.

## Setup
| | node-1 | node-2 | workstation |
| --- | --- | --- | --- |
| GPU | GeForce GTX 860M, 4 GB, compute 5.0 | GeForce GTX 960M, 4 GB, compute 5.0 | RTX 3060 Laptop, 6 GB (≈3.9 GB for vLLM), compute 8.6 |
| Driver | 580.178.04 | 580.173.02 | 592.82 (Windows, WSL2) |
| Host | 8 CPU, 8 GB RAM, Ubuntu 24.04 | 4 CPU, 7.4 GB RAM, Ubuntu 26.04 | Docker Desktop 4.98 |
| Engine | llama-server b12063, CUDA 12.8.1 | llama-server b12063, CUDA 12.8.1 | vLLM 0.32.0, CUDA 13.0 |
| Model file | SmolLM3-3B Q4_K_M GGUF (ggml-org) | same | SmolLM3-3B W4A16 (aleada) |
| Settings | all layers on GPU, 2 slots × 8,192 context | same | max 8,192 context, max 32 sequences, 0.65 GPU memory |

- **Client:** `tollgate_bench` as a Kubernetes Job on the node *not* being measured (vLLM and both-nodes
  runs: node-1). It used about 5 millicores and 40 MiB. The node-1 and node-2 runs ran at the same time,
  each on its own laptop.
- **Requests:** a synthetic support ticket starting with a unique ID; `max_tokens: 128` with `ignore_eos`,
  so every answer is exactly 128 tokens; temperature 0; thinking off. Token counts are the servers'.
- **Levels:** 10, 32, 64 and 128 requests at 1, 8, 32 and 64 users, after 2 warm-up requests. One run
  per level, so run-to-run variance isn't measured.
- **Time per token** is the generation phase only: (latency − TTFT) ÷ 127.
- **GPU columns** come from Prometheus (DCGM exporter) over each level's window. The workstation GPU
  isn't scraped.

## All results

### llama-server-0 (node-2, GTX 960M)

| Users | OK / sent | Output tok/s | Req/s | TTFT p50 | TTFT p95 | Time per token p50 | Time per token p95 | GPU max | GPU min clock |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 / 10 | 11.7 | 0.09 | 1.8 s | 1.8 s | 72 ms | 73 ms | 69 °C | 1202 MHz |
| 8 | 31 / 32 | 20.2 | 0.16 | 40.0 s | 40.3 s | 74 ms | 74 ms | 72 °C | 1202 MHz |
| 32 | 64 / 64 | 20.7 | 0.16 | 187.9 s | 188.5 s | 74 ms | 74 ms | 74 °C | 1202 MHz |
| 64 | 128 / 128 | 20.7 | 0.16 | 385.6 s | 387.0 s | 74 ms | 74 ms | 74 °C | 1202 MHz |

### llama-server-1 (node-1, GTX 860M)

| Users | OK / sent | Output tok/s | Req/s | TTFT p50 | TTFT p95 | Time per token p50 | Time per token p95 | GPU max | GPU min clock |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 / 10 | 10.0 | 0.08 | 2.1 s | 2.1 s | 84 ms | 85 ms | 75 °C | 1097 MHz |
| 8 | 31 / 32 | 17.1 | 0.13 | 47.3 s | 47.7 s | 87 ms | 88 ms | 82 °C | 1097 MHz |
| 32 | 63 / 64 | 17.2 | 0.13 | 224.1 s | 224.8 s | 88 ms | 89 ms | 86 °C | 1097 MHz |
| 64 | 127 / 128 | 17.3 | 0.13 | 459.7 s | 462.0 s | 89 ms | 89 ms | 82 °C | 1097 MHz |

### llama-server Service (both nodes)

| Users | OK / sent | Output tok/s | Req/s | TTFT p50 | TTFT p95 | Time per token p50 | Time per token p95 | GPU max | GPU min clock |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 / 10 | 10.9 | 0.09 | 2.0 s | 2.1 s | 72 ms | 84 ms | 64 °C | 135 MHz |
| 8 | 32 / 32 | 35.6 | 0.28 | 14.8 s | 32.6 s | 87 ms | 88 ms | 82 °C | 135 MHz |
| 32 | 63 / 64 | 29.0 | 0.23 | 64.8 s | 151.1 s | 88 ms | 89 ms | 85 °C | 135 MHz |
| 64 | 127 / 128 | 34.5 | 0.27 | 170.3 s | 256.9 s | 88 ms | 89 ms | 86 °C | 135 MHz |

### vLLM (workstation, RTX 3060 Laptop)

| Users | OK / sent | Output tok/s | Req/s | TTFT p50 | TTFT p95 | Time per token p50 | Time per token p95 | GPU max | GPU min clock |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 / 10 | 113.4 | 0.89 | 77 ms | 82 ms | 8 ms | 8 ms | – | – |
| 8 | 32 / 32 | 613.2 | 4.79 | 454 ms | 477 ms | 9 ms | 12 ms | – | – |
| 32 | 64 / 64 | 1087.6 | 8.50 | 1.1 s | 1.8 s | 20 ms | 24 ms | – | – |
| 64 | 128 / 128 | 1103.0 | 8.62 | 4.5 s | 5.4 s | 22 ms | 26 ms | – | – |

## Two tenants, one set of GPUs (week 9, Phase 2 gate)

Fair queuing (ADR 0014), measured on 2026-11-07 with `python -m tollgate_bench fairness`.

**Setup:**
- The gateway's 2 replicas in front of the two llama-server nodes: 4 slots in all, vLLM off.
- `support` sends 12 requests at once through one gateway replica. `billing` sends one request
  every 5 s, from 2 s on, through the other replica.
- Every request uses the benchmark's prompt (~256 tokens) and 128 output tokens.
- Run once with `TOLLGATE_FAIR_QUEUE=false` (requests queue first-come inside llama-server) and once
  with it on.

| Run | Tenant | OK / sent | 429s | First token p50 | First token worst | All done |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| fair queue off | support (burst) | 12 / 12 | 0 | 17.8 s | 33.0 s | 44.1 s |
| fair queue off | billing (trickle) | 5 / 5 | 0 | 32.4 s | 40.6 s | 63.6 s |
| fair queue on | support (burst) | 12 / 12 | 0 | 26.4 s | 53.0 s | 62.3 s |
| fair queue on | billing (trickle) | 5 / 5 | 0 | 13.8 s | 18.0 s | 51.2 s |

**What it says:**
- **`billing` waits much less behind someone else's burst:** its median wait for a first token
  drops from 32.4 s to 13.8 s, and its worst from 40.6 s to 18.0 s.
- **Throughput doesn't change.** All 17 requests are done in 62–64 s either way. The burst finishes
  later (44 → 62 s) because it now shares the slots.
- **Each tenant holds 2 of the 4 slots while both are waiting.** `billing`'s remaining wait is mostly
  generation time: one request every 5 s is about its whole share (2 slots, ~12 s per request), so
  its requests also wait for each other. A lighter tenant would only wait for the next free slot.
- **The two replicas share one queue.** The burst and the trickle came in through different gateway
  replicas, and the queue in Redis still kept them fair.
- **Each tenant has its own token limit.** With the limit lowered to 1,200 tokens a minute, `billing`'s
  5 overlapping requests got 2 × 200 and 3 × 429 (`rate_limit_exceeded`, with `Retry-After`). A
  `support` request sent at the same moment got 200 from its own, full bucket.
- **The first run found a bug.** `billing` waited 16.7 s median, 24.3 s worst, because admitted
  requests were routed by `/metrics` that could be a second old: some went to a node that was still
  full and waited there for a whole generation. The queue now hands out a slot on a specific backend.

To run it again (keys for two tenants in `TG_FLOOD` and `TG_TRICKLE`, a port-forward to each gateway
pod on 8001 and 8002):
```
uv run python -m tollgate_bench fairness --url http://localhost:8001 --trickle-url http://localhost:8002 \
  --label "fair queue on" --flood-key-env TG_FLOOD --trickle-key-env TG_TRICKLE > bench/raw/fairness-on.jsonl
uv run python -m tollgate_bench report bench/raw/fairness-*.jsonl
```
