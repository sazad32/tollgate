# Dev log

Newest entry at the top. Each entry: what changed, what broke, what was measured.

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
