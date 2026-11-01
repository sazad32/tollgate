# bench

Load and benchmark scripts, and their results.

- **Week 4:** measure time-to-first-token, tokens/sec and p50/p99 latency at 1, 8, 32 and 64 concurrent
  streaming requests against the vLLM Service. Fixed prompts, fixed `max_tokens`, 3 runs each.
- Results go in `RESULTS.md`, with the hardware and model noted.
- Raw run output goes in `bench/raw/` (gitignored).
- **Phase 4:** re-run through the gateway under load and record before/after numbers.
