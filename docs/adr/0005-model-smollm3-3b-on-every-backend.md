# 0005 — Model: SmolLM3-3B on every backend

- Status: accepted
- Date: 2026-11-03

## Context
ADR 0004 gives us two kinds of local backend:
- `llama-server` on two GTX 860M/960M nodes: 4 GB VRAM each, compute capability 5.0, CUDA 12 builds.
- vLLM on the workstation's RTX 3060 Laptop GPU: 6 GB, but Windows uses about 1.5 GB of it for the
  display, so vLLM gets roughly 4.2 GB.

We want one open instruct model that fits both, under a license a telecom could use commercially.
Using the same model everywhere means the week 4 benchmark compares engines, not models, and the
gateway sees one model name whichever backend answers.

GPU memory needed = weights + KV cache + runtime overhead. The KV cache (the per-token attention
state) grows with context: bytes per token = 2 (keys and values) × layers × KV heads × head size × 2 bytes.

## Options considered
Sizes are the 4-bit files we would actually run. Node and workstation figures are estimates.

| | SmolLM3-3B | Granite 4.2 3B | Qwen3-4B-Instruct-2507 |
| --- | --- | --- | --- |
| License | Apache-2.0; training data and recipe published | Apache-2.0 | Apache-2.0 |
| llama.cpp file | Q4_K_M, 1.8 GB (ggml-org) | Q4_K_M, 2.1 GB (IBM) | Q4_K_M, 2.3 GB (unsloth) |
| vLLM file | W4A16: Red Hat's 2.3 GB build (needs vLLM ≤ 0.29, see below) or a 1.8 GB community build | W4A16, 2.6 GB, community upload only; IBM's FP8 is 3.9 GB | W4A16, 3.2 GB (output layer kept at 16-bit) |
| KV cache per token | 72 KB | 80 KB | 144 KB |
| Node (4 GB) | ≈3.3 GB with 16k tokens of KV | ≈3.1 GB at 8k | ≈3.3 GB at 4k only |
| Workstation (≈4.2 GB) | fits, about 1 GB left for KV | tight, about 0.8 GB for KV | doesn't fit next to the desktop |
| Main downside | weakest of the three | vLLM build from an unknown uploader | only fits the nodes, short context |

Also ruled out:
- **Gemma 4 E2B/E4B:** the vLLM builds are 7.8 GB or more, because of per-layer embeddings.
- **Qwen3.5-4B:** its vision tower and 248k-token vocabulary push the 4-bit build past the
  workstation, and its hybrid attention layers are untested on compute capability 5.0.
- **Llama 3.2 3B:** gated download, custom license, and a heavy KV cache (112 KB per token).

## Decision
SmolLM3-3B on every local backend, served under the name `smollm3-3b`:
- **llama-server:** `SmolLM3-Q4_K_M.gguf` from `ggml-org/SmolLM3-3B-GGUF`, pinned to a commit and
  checked against its SHA-256. Two slots of 8k tokens each.
- **vLLM:** `aleada/SmolLM3-3B-W4A16` on vLLM v0.32.0, pinned to a commit. Max context 8k.
- **Thinking off by default** on both. A request can still turn it on.

We first chose Red Hat's build (`RedHatAI/SmolLM3-3B-quantized.w4a16`). vLLM v0.32.0 refuses it: the
build uses group activation ordering (`actorder: group`, a GPTQ setting), which vLLM removed in v0.30.0.
The choice was then between pinning vLLM 0.29 to keep a trusted source, or keeping current vLLM with a
community build that uses the supported `static` ordering. We chose current vLLM. The community build is
weights and tokenizer files only (safetensors, no code), so the risk is quality, not code execution, and
quality can be measured against `llama-server`.

Checked against the official `HuggingFaceTB/SmolLM3-3B` files: the chat template is identical, and the
tokenizer's vocabulary, merges and special tokens are identical. The one difference is a leftover
truncation setting (2,048 tokens) in `tokenizer.json`, probably from the uploader's calibration run. vLLM
ignores it: a 3,288-token prompt counted as 3,288 tokens on both engines, and the model could still read
its first line.

## Consequences
- Quality is modest. Summaries, classification and short answers are fine; hard requests go to the
  hosted fallback, which is what the week 8 router is for.
- The two engines run different 4-bit formats of the same model (GGUF Q4_K_M and W4A16), so outputs
  differ slightly. Benchmarks state the format along with the hardware.
- Thinking off keeps output length predictable, which the week 7 token reservation needs. A request
  that turns thinking on produces, and is billed for, extra output tokens.
- On the nodes, the same 16k-token KV budget could be 2 slots × 8k or 4 slots × 4k. We chose 2 × 8k
  to match vLLM's max context. Revisit after the week 4 benchmark.
- The model is configuration: `deploy/llama-server/llama-server.env` and
  `deploy/workstation/vllm/compose.yaml`. Swapping it is an edit and a re-apply.
- The vLLM build comes from an unknown uploader. That weakens the "trusted source" argument used
  against Granite. The proper fix is to quantize the official weights ourselves with llm-compressor and
  a committed recipe. That adds a tool, so it needs its own decision.
- Both engines count the same prompt as the same number of tokens (80 for the test prompt): same
  tokenizer and chat template, so metering doesn't depend on which backend answered.
- Revisit if the workstation's display moves off the RTX 3060 (Qwen3-4B would then fit), if IBM
  publishes a 4-bit Granite build for vLLM, or once we quantize our own build.

## Measured (week 2, single requests)
These are smoke-test numbers, not the week 4 benchmark. The long prompt is 1,278 tokens.

| | node-1 (GTX 860M) | node-2 (GTX 960M) | workstation (RTX 3060 Laptop) |
| --- | --- | --- | --- |
| Engine | llama-server b12063 | llama-server b12063 | vLLM 0.32.0 |
| GPU memory used by the server | 3.1 of 4 GB | 3.1 of 4 GB | 3.9 GB budget: 1.9 GB weights, 1.3 GB KV cache (18,512 tokens) |
| Prompt processing, long prompt | 135 tok/s | 161 tok/s | about 2,200 tok/s (0.58 s to first token) |
| Generation | 11.6 tok/s | 13.7 tok/s | about 108 tok/s |
| Cold start (download included) | download 145 s, load 35 s | download 85 s, load 58 s | about 210 s |
| Restart | 15 s | 15 s | about 90 s |

On the nodes, most of the first load is the driver compiling GPU kernels for compute capability 5.0. The
compiled kernels are cached on the model volume, and the warm-up step drops from 30 s to 0.3 s on restart.
