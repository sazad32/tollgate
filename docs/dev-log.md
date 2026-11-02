# Dev log

Newest entry at the top. Each entry: what changed, what broke, what was measured.

## 2026-11-01 — Cluster prep
- Changed: SSH key auth to both nodes; kubectl access from the workstation (`tollgate` context);
  NVIDIA driver packages held on both nodes; ADR 0004 (llama-server on the nodes, vLLM on the workstation).
- Broke: node-1 had an NVIDIA driver/library version mismatch until a reboot. The node GPUs are compute
  capability 5.0, too old for vLLM, which led to ADR 0004. Docker Desktop's kubectl (v1.29) is too old for
  the v1.35 cluster; still open.
- Measured: both nodes Ready, one GPU each.

## 2026-11-01 — Week 1: repo scaffold
- Changed: uv workspace with `gateway` and `billing` packages (src layout, one smoke test each);
  shared Ruff + pytest config; GitHub Actions CI (lint, format check, tests); Apache-2.0 LICENSE;
  `deploy/` and `bench/` placeholders; public repo on GitHub.
- Broke: first CI run failed because `astral-sh/setup-uv` has no floating `v10` tag, so it's pinned to
  `v10.3.3`. The gh token also lacked the `workflow` scope needed to push workflow files.
- Measured: 2/2 tests pass; CI green in 10s.

## 2026-10-31 — Kickoff
- Changed: project brief, plan, architecture and first three ADRs written.
- Broke: nothing yet.
- Measured: nothing yet. Next: week 1 scaffold.
