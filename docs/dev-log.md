# Dev log

Newest entry at the top. Each entry: what changed, what broke, what was measured.

## 2026-11-07 — Week 3: monitoring
- Changed: kube-prometheus-stack via k3s's Helm controller (ADR 0007); DCGM GPU exporter; scrape targets
  for llama-server and vLLM (via a Service with no pods behind it); dashboards: Model servers, GPUs.
- Broke: DCGM exporter OOM-killed at a 384 MiB limit (uses ~420); node-2's uutils `install` rejected a
  numeric group; Grafana 13 is distroless, so no in-container checks.
- Measured: 2 parallel requests ~double node output (26.5 / 20.4 tok/s); vLLM 359 tok/s at 4; monitoring
  ~1.4 GB RAM; node-1 GPU peaked at 82 °C.

## 2026-11-03 — Week 2: model serving
- Changed: SmolLM3-3B on every backend (ADR 0005); `tollgate-local` StorageClass with one PV per node
  (ADR 0006); llama-server StatefulSet, one pod per GPU node, plus Services; vLLM compose on the workstation.
- Broke: vLLM 0.30+ rejects Red Hat's build (`actorder: group`), so we use a community one. Docker Desktop
  4.30 broke CUDA in containers until updated. Docker's firewall rule exposed its ports on Public networks.
- Measured: generation 12–14 tok/s per node, ~108 tok/s on vLLM; node pod restart 15 s.

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
