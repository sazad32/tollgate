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

## Metered exactly: what the clients saw, what the ledger billed (week 13, Phase 3 gate)

Measured on 2026-11-08, 21:09 to 21:12 UTC, with `python -m tollgate_bench metering` (ADR 0018).

**Setup:**
- The gateway in front of the two llama-server nodes (4 slots), vLLM off, nothing else running.
- 100 requests each from `support` and `billing`, two at a time per tenant:
  - half plain;
  - four in ten streamed;
  - one in ten streamed and hung up on after 5 chunks.
- Each prompt is unique, with `max_tokens` 32 and `ignore_eos`, so each completed request generates
  exactly 32 tokens.
- Every client record carries the gateway's request ID (`X-Request-ID`) and the usage the client
  was told.

| Check | Result |
| --- | --- |
| Requests | 200 sent: 180 completed, 20 hung up, 0 errors, in 198 s |
| Each request against its ledger row (`check-requests`) | All 200 found. The 180 completed requests were billed exactly the usage the client saw; the 20 hang-ups were billed their estimate. |
| Ledger rows | 100 per tenant, 100 distinct keys each: none lost, none twice |
| Ledger vs invoice preview | Equal for both tenants: support 10,539 in / 3,419 out, billing 9,410 / 3,047 (November so far) |
| Ledger vs the servers' own counters, output (`reconcile`) | llama-server-0: 3,323 vs 3,338. llama-server-1: 2,769 vs 2,785. Total: ledger 6,092, servers 6,123, +31 |

**What it says:**
- **Completed requests are metered exactly.** The ledger's reported output is 5,760 = 180 × 32,
  and the servers' counters agree.
- **The whole difference (+31 tokens) is the 20 hang-ups.** They generated 363 tokens and were
  estimated at 332, about 8.5% low: tokens still in flight when the gateway cancelled. That's within
  the reconciliation's tolerance (half of the estimated tokens, 165).
- **Input tokens can't be checked this way.** llama-server's prompt counter skips prompt tokens it
  had cached, so it saw far fewer than the requests' real prompts. The ledger's input comes from
  the server's own usage report, which the audit matched request by request.

To run it again (each tenant's API key in an environment variable, a port-forward to the gateway
on 8000; the run's start and end times are in its log):
```
uv run python -m tollgate_bench metering --url http://localhost:8000 --key support=TG_SUPPORT \
  --key billing=TG_BILLING > bench/raw/metering.jsonl
kubectl -n tollgate exec -i deploy/tollgate-billing -- python -m tollgate_billing check-requests < bench/raw/metering.jsonl
kubectl -n tollgate exec deploy/tollgate-billing -- python -m tollgate_billing reconcile --start <start> --end <end, 30 s after the run>
```

## Where async Python breaks (week 14, Phase 4)

Measured on 2026-11-09 with `python -m tollgate_bench load` (ADR 0019). The gateway sent to a fake
model server: exactly 256 tokens per request, the first after 100 ms, then 20 a second. The GPUs
were out of the way. The clients ran as 7 pods, about 150 streams each. Each step held its number
of open streams for 45 s.

**The clients and the fake server alone** (no gateway): clean up to 1,000 streams.

| Run | Streams | Starts/s | OK / sent | Errors | First token p50 / p99 | Token gap p50 / p99 / p99.9 | Max open | Client late |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| client -> mock | 250 | 19.5 | 882 / 882 | – | 110 / 120 | 51 / 51 / 52 | 259 | 0 starts, lag 90 ms |
| client -> mock | 500 | 38.9 | 1,757 / 1,757 | – | 110 / 120 | 50 / 52 / 59 | 504 | 0 starts, lag 29 ms |
| client -> mock | 1,000 | 77.8 | 3,507 / 3,507 | – | 110 / 140 | 50 / 58 / 84 | 1,008 | 0 starts, lag 58 ms |

**Through the gateway** (the load-test copy, same image and resources as production):

| Run | Streams | Starts/s | OK / sent | Errors | First token p50 / p99 | Token gap p50 / p99 / p99.9 | Max open | Client late |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| gateway, 1 replica | 100 | 7.8 | 357 / 357 | – | 140 / 560 | 50 / 68 / 150 | 105 | 0 starts, lag 111 ms |
| gateway, 1 replica | 250 | 19.5 | 790 / 882 | RemoteProtocolError ×92 | 4,000 / 62,000 | 51 / 250 / 550 | 423 | 0 starts, lag 11 ms |
| gateway, 1 replica | 500 | 38.9 | 511 / 1,757 | HTTP 429 ×195, RemoteProtocolError ×1051 | 15,000 / 65,000 | 1 / 620 / 2,100 | 1,388 | 0 starts, lag 5 ms |
| gateway, 1 replica | 1,000 | 77.8 | 278 / 3,507 | ConnectError ×119, HTTP 429 ×719, HTTP 503 ×444, HTTP 504 ×197, ReadError ×272, ReadTimeout ×6, RemoteProtocolError ×1472 (out of memory, restarted) | 6,100 / 27,000 | 1 / 1,100 / 2,400 | 1,750 | 7 starts, lag 201 ms |
| gateway, 2 replicas | 100 | 7.8 | 357 / 357 | – | 130 / 430 | 50 / 66 / 150 | 105 | 0 starts, lag 107 ms |
| gateway, 2 replicas | 250 | 19.5 | 882 / 882 | – | 150 / 320 | 50 / 88 / 160 | 259 | 0 starts, lag 3 ms |
| gateway, 2 replicas | 500 | 38.9 | 1,739 / 1,757 | ReadTimeout ×18 | 4,400 / 112,000 | 51 / 250 / 730 | 811 | 0 starts, lag 6 ms |
| gateway, 2 replicas | 1,000 | 77.8 | 2,661 / 3,507 | HTTP 429 ×571, HTTP 503 ×267, HTTP 504 ×1, ReadTimeout ×7 | 50,000 / 114,000 | 1 / 520 / 1,200 | 2,253 | 0 starts, lag 23 ms |

**Inside the gateway** (its own metrics, one replica; p99 over a minute):

| Streams | CPU (cores) | Event-loop lag | Key check | Redis rate limit | Fair queue | Model's headers |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 0.24 | 12 ms | < 1 ms | 10 ms | 23 ms | 50 ms |
| 250 | 0.60 | 70 ms | 118 ms | 70 ms | 177 ms | 1.9 s |
| 500 | 0.87–1.0 | 380–500 ms | 1.7 s | 0.4–0.9 s | 0.5–1.3 s | ≥ 5 s |
| 1,000 | ~0.9, then out of memory | 2.1 s | 3.2 s | 1.0 s | 2.2 s | ≥ 5 s |

**What it says:**
- **The latency targets aren't met at any load.** At 2 replicas and 250 streams, the gateway adds
  about 200 ms to p99 time to first token (target 50) and about 37 ms to p99 token gaps (target
  10).
- **Two replicas hold 250 streams without errors, and fail at 500**: a quarter of the 1,000-stream
  target.
- **The first limit is a setting, not Python.** The upstream connection pool allows 200
  connections per replica, so stream 201 waits up to 10 s for one.
- **Then one core runs out.** A replica's single event loop is full at about 500 streams (10,000
  tokens a second, each read from the model and written to the client). Its lag then slows
  everything: even a cached key check takes 1.7 s.
- **Under load, rate limits and the fair queue switch themselves off.** redis-py's pool of 100
  connections runs dry, and the gateway fails open, as designed for a Redis outage. Queue slots
  it can't release leak for 30 s.
- **Nothing pushes back.** In-flight requests grow until the pod runs out of memory, and every
  connection drops at once.
- **Billing kept up.** The stream's backlog stayed at 0, with every request a model ran in the
  ledger once.
- **The client is a limit too.** One client process fills a core at about 250–300 streams, which
  is why the clients ran 7 pods at about 150 each.

To run it again (bench/README.md, "Load test"): apply `deploy/loadtest`, then run `load-job` for
the fake server alone and for the gateway, and `load-report` on the logs.

## Fixes, before and after (week 15, Phase 4)

Measured 2026-11-12 to 2026-11-15, with the same fake server and the same steps as week 14 (256
tokens per request at 20 a second, 45 s per step). ADR 0020 has the fixes and why.

**Same setup as week 14:** clients and fake server on node-1, gateway on node-2. Two replicas,
after the pool, Redis, backpressure, uvloop and httptools fixes, but before netio and the meter
shortcut. Each replica takes at most 300 streams (`TOLLGATE_MAX_IN_FLIGHT`).

| Run | Streams | Starts/s | OK / sent | Errors | First token p50 / p99 | Token gap p50 / p99 / p99.9 | Max open | Client late |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| client -> mock | 250 | 19.5 | 882 / 882 | – | 110 / 120 | 51 / 52 / 52 | 259 | 0 starts, lag 112 ms |
| client -> mock | 500 | 38.9 | 1,757 / 1,757 | – | 110 / 120 | 50 / 52 / 57 | 504 | 0 starts, lag 48 ms |
| client -> mock | 1,000 | 77.8 | 3,507 / 3,507 | – | 110 / 140 | 50 / 57 / 88 | 1,008 | 0 starts, lag 29 ms |
| 2 replicas, fixes, cap 300 | 100 | 7.8 | 357 / 357 | – | 150 / 520 | 51 / 75 / 120 | 105 | 0 starts, lag 105 ms |
| 2 replicas, fixes, cap 300 | 250 | 19.5 | 882 / 882 | – | 150 / 250 | 50 / 81 / 160 | 259 | 0 starts, lag 29 ms |
| 2 replicas, fixes, cap 300 | 500 | 38.9 | 1,754 / 1,757 | HTTP 504 ×3 | 250 / 720 | 47 / 300 / 430 | 533 | 0 starts, lag 32 ms |
| 2 replicas, fixes, cap 300 | 1,000 | 77.8 | 2,064 / 3,507 | HTTP 503 ×1437, HTTP 504 ×6 | 510 / 1,700 | 47 / 340 / 490 | 659 | 0 starts, lag 47 ms |

Against week 14's 2 replicas: at 250 streams the gateway adds 130 ms to p99 first token (was 200)
and 29 ms to p99 token gaps (was 37). At 500, 3 errors (was 18) and a p99 first token of 0.7 s
(was 112 s). At 1,000, the cap turns the excess away with quick 503s; in week 14 the replicas
ran out of memory.

**The link between the nodes counted as the gateway's.** The same image (d9658d5), one replica:

| Where | Streams | First token p99 | Token gap p99 |
| --- | ---: | ---: | ---: |
| gateway on node-2, clients and fake server on node-1 | 100 | 280 | 76 |
| everything on node-2 | 100 | 240 | 55 |
| straight to the fake server, on node-2 | 100 | 220 | 53 |
| gateway on node-2, clients and fake server on node-1 | 200 | 390 | 95 |
| everything on node-2 | 200 | 230 | 59 |
| straight to the fake server, on node-2 | 200 | 190 | 55 |

A TCP round trip between the nodes takes about 4 ms at the median, 5–13 ms at p90 and up to
36 ms; within a node, 0.3 ms. Through the gateway every token crossed that link twice, and
straight to the fake server never. From here on, every run is **node-local**: each request stays
on one node, from client to gateway to fake server (ADR 0020).

**Node-local:** a fake server on each node, 7 single-process gateway pods (5 on node-1, 2 on
node-2) and 7 client pods split the same way. Image d9658d5 (netio and the meter shortcut).
Run 1:

| Run | Streams | Starts/s | OK / sent | Errors | First token p50 / p99 | Token gap p50 / p99 / p99.9 | Max open | Client late |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| client -> mock, node-local | 250 | 19.5 | 882 / 882 | – | 110 / 230 | 51 / 52 / 63 | 259 | 0 starts, lag 70 ms |
| client -> mock, node-local | 500 | 38.9 | 1,757 / 1,757 | – | 110 / 190 | 51 / 52 / 61 | 506 | 0 starts, lag 19 ms |
| client -> mock, node-local | 750 | 58.4 | 2,632 / 2,632 | – | 110 / 150 | 51 / 53 / 75 | 758 | 0 starts, lag 46 ms |
| client -> mock, node-local | 1,000 | 77.8 | 3,507 / 3,507 | – | 110 / 130 | 51 / 53 / 66 | 1,008 | 0 starts, lag 29 ms |
| gateway, 7 processes, node-local | 250 | 19.5 | 882 / 882 | – | 140 / 330 | 51 / 56 / 120 | 260 | 0 starts, lag 83 ms |
| gateway, 7 processes, node-local | 500 | 38.9 | 1,757 / 1,757 | – | 140 / 220 | 51 / 56 / 84 | 513 | 0 starts, lag 35 ms |
| gateway, 7 processes, node-local | 750 | 58.4 | 2,632 / 2,632 | – | 140 / 300 | 50 / 75 / 180 | 768 | 0 starts, lag 52 ms |
| gateway, 7 processes, node-local | 1,000 | 77.8 | 3,507 / 3,507 | – | 450 / 1,400 | 51 / 140 / 230 | 1,087 | 0 starts, lag 72 ms |

Run 2, with a cheaper setup (clients on uvloop; the fake server on uvloop and httptools). At 1,000
streams the setup itself took 4.6 cores instead of 5.6:

| Run | Streams | Starts/s | OK / sent | Errors | First token p50 / p99 | Token gap p50 / p99 / p99.9 | Max open | Client late |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| client -> mock, node-local | 250 | 19.5 | 882 / 882 | – | 110 / 230 | 51 / 52 / 54 | 259 | 0 starts, lag 54 ms |
| client -> mock, node-local | 500 | 38.9 | 1,757 / 1,757 | – | 110 / 170 | 51 / 52 / 55 | 506 | 0 starts, lag 25 ms |
| client -> mock, node-local | 750 | 58.4 | 2,632 / 2,632 | – | 110 / 120 | 51 / 52 / 57 | 757 | 0 starts, lag 6 ms |
| client -> mock, node-local | 1,000 | 77.8 | 3,507 / 3,507 | – | 110 / 130 | 51 / 53 / 67 | 1,007 | 0 starts, lag 48 ms |
| gateway, 7 processes, node-local | 250 | 19.5 | 882 / 882 | – | 140 / 420 | 51 / 55 / 120 | 264 | 0 starts, lag 60 ms |
| gateway, 7 processes, node-local | 500 | 38.9 | 1,756 / 1,757 | HTTP 504 ×1 | 130 / 350 | 51 / 55 / 71 | 523 | 0 starts, lag 9 ms |
| gateway, 7 processes, node-local | 750 | 58.4 | 2,632 / 2,632 | – | 130 / 230 | 50 / 59 / 170 | 764 | 0 starts, lag 9 ms |
| gateway, 7 processes, node-local | 1,000 | 77.8 | 3,507 / 3,507 | – | 170 / 820 | 50 / 110 / 150 | 1,048 | 0 starts, lag 39 ms |

**What the gateway adds** (its p99 minus the fake server's, same step; targets: 50 ms first token,
10 ms token gap):

| Streams | Run 1: first token / token gap | Run 2: first token / token gap |
| ---: | ---: | ---: |
| 250 | +100 / **+4** | +190 / **+3** |
| 500 | **+30** / **+4** | +180 / **+3** |
| 750 | +150 / +22 | +110 / **+7** |
| 1,000 | +1,270 / +87 | +690 / +57 |

**Inside the gateway** (its own metrics, run 2, over each step's last 30 s; timings are p99s
across all 7 pods, CPU and memory the busiest pod's):

| Streams | CPU (cores) | Memory | Event-loop lag | Key check | Redis rate limit | Fair queue | Model's headers |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 250 | 0.43 | 122 MiB | 19 ms | < 1 ms | 55 ms | 49 ms | 43 ms |
| 500 | 0.43 | 126 MiB | 14 ms | < 1 ms | 29 ms | 49 ms | 19 ms |
| 750 | 0.62 | 131 MiB | 7 ms | 35 ms | 24 ms | 43 ms | 24 ms |
| 1,000 | 0.74 | 133 MiB | 223 ms | < 1 ms | 237 ms | 379 ms | 401 ms |

No step failed open on Redis in either run.

**What it says:**
- **The fixes held where week 14 broke.** No pool runs dry, nothing runs out of memory, and the
  rate limits and fair queue stay on. Errors went from hundreds to one in about 17,500 requests.
- **Token gaps are within target up to 500 streams,** and up to 750 in run 2.
- **First tokens are not.** Before its first token, a request makes 3 round trips to Redis:
  reserve tokens, join the fair queue, take a slot. Redis is on node-2, so for node-1's gateways
  (70% of the traffic) each crosses the slow link. The rate limit and the queue each take
  25–105 ms at p99 even at 250–750 streams.
- **1,000 streams don't fit the cluster beside the test setup.** One gateway process holds the
  targets up to about 100 streams, at about 0.45 cores; 1,000 need about 4.5 cores of gateway on
  top of the setup's 4.6. At 1,000, node-1 had 7.1 of its 8 CPUs busy.
- **The Phase 4 gate is not met.** Next: one Redis round trip instead of three, and the link
  between the nodes (ADR 0020, "Still open").

To run it again (bench/README.md, "Load test"): apply `deploy/loadtest`, then run `load-job` for
the fake server alone and for the gateway, and `load-report` on the logs. Steps 250, 500, 750 and
1,000.
