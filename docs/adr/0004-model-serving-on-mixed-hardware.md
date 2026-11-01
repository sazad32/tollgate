# 0004 — Model serving: llama.cpp on the cluster nodes, vLLM on newer GPUs

- Status: accepted
- Date: 2026-11-01

## Context
The plan assumed vLLM on both k3s nodes. The nodes are older gaming laptops with a GeForce GTX 860M
(node-1) and GTX 960M (node-2): both 4 GB VRAM, compute capability 5.0 (Maxwell). vLLM needs compute capability 7.0 or newer, so it can't run
on them. CUDA 13 dropped support for Maxwell, and the 580 driver branch is the last one that supports it.

The owner's workstation has an RTX 3060 Laptop GPU (6 GB VRAM, compute capability 8.6), which vLLM
supports. It isn't always on and isn't part of the cluster. There's no budget for new hardware.

## Options considered
1. Buy a newer GPU: keeps the original plan, costs money.
2. Cloud GPU only: vLLM on 24 GB GPUs, billed by the hour. You pay for idle time, and nothing runs in the
   home network.
3. llama.cpp everywhere: runs on all the hardware, but skips vLLM, the industry-standard serving engine.
4. Mixed fleet: llama.cpp's `llama-server` on the nodes in k3s, vLLM in Docker on the workstation, and
   an optional cloud GPU rented by the hour for headline benchmarks.

## Decision
Option 4. Every backend exposes the same OpenAI-compatible API, so the gateway treats them as
interchangeable upstreams.

## Consequences
- Only small models fit: about 3–4B parameters at 4-bit. Throughput will be modest, and every benchmark
  states its hardware.
- The week 8 router has to handle different backends: different models, different capacity, and a
  workstation backend that may be offline. That's a good fallback test.
- `llama-server` images must be CUDA 12 builds that include compute capability 5.0. Pin and verify the
  image version.
- vLLM and `llama-server` expose different Prometheus metrics, so the dashboards need both.
- Cloud GPU runs need a spend cap and a teardown checklist, because idle time costs money.
- Revisit if newer GPU hardware becomes available.
