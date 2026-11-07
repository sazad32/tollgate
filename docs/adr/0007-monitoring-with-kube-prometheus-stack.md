# 0007 — Monitoring: kube-prometheus-stack, DCGM exporter, our own dashboards

- Status: accepted
- Date: 2026-11-07

## Context
Week 3 needs metrics from the model servers (two `llama-server` replicas in k3s, vLLM on the workstation)
and from the node GPUs, in Prometheus and Grafana (both in the stack). Constraints:
- No Helm CLI on the workstation, and the cluster is k3s.
- 8 GB of RAM per node, already running the model servers.
- The node GPUs are GeForce Maxwell cards (GTX 860M / 960M). NVIDIA's DCGM officially supports
  datacenter GPUs only.
- vLLM runs outside the cluster on a machine that's often off, and its LAN address must stay out of
  this public repo.

## Options considered
1. **Install:** kube-prometheus-stack through the Helm CLI, through k3s's built-in Helm controller (a
   `HelmChart` object), or hand-written Prometheus and Grafana manifests. The Helm CLI is portable but
   one more tool and an imperative step. Hand-written manifests mean maintaining the operator, RBAC and
   CRDs ourselves.
2. **GPU metrics:** NVIDIA's DCGM exporter (official, rich, pod attribution, but datacenter-only on
   paper), or a community exporter that wraps `nvidia-smi` (works on any card, third-party dependency).
3. **Reaching vLLM:** a static scrape target with its IP in Git, a hand-made Secret with extra scrape
   config, or a selector-less Service whose address lives in a hand-applied EndpointSlice.
4. **Dashboards:** import large community dashboards, or write small ones ourselves.

## Decision
- **kube-prometheus-stack 92.22.0, installed by k3s's Helm controller** from
  `deploy/monitoring/kube-prometheus-stack.yaml`, applied with the rest of `deploy/`. Trimmed for k3s
  and small nodes: Alertmanager off, control-plane scrapes that k3s doesn't expose turned off.
  Prometheus keeps 15 days (15 GB cap) on a local PV on node-2's data disk and scrapes every 15 s.
  Grafana has no storage of its own; its login comes from a hand-made Secret.
- **DCGM exporter 4.9.0**, after testing it on both cards: GPU and memory utilisation, VRAM,
  temperature and clocks work, and each GPU's metrics name the pod using it. Power and the profiling
  metrics aren't available on these cards, so it runs without `SYS_ADMIN`.
- **A selector-less `vllm-workstation` Service** plus an EndpointSlice kept in a gitignored
  `*.local.yaml` file. Prometheus discovers targets through EndpointSlices so it can see it. The same
  Service gives the week 8 router a stable in-cluster name for vLLM.
- **Two small dashboards of our own**, committed as JSON and loaded by Grafana's sidecar: *Model
  servers* (both engines side by side, labelled by `backend`) and *GPUs*. Each backend and node keeps a
  fixed colour, so a series doesn't change colour when vLLM goes offline.

## Consequences
- One `kubectl apply -k deploy` installs everything, but a fresh cluster needs it run twice: the
  monitors need the operator's CRDs, which the chart installs in a background Job.
- The `HelmChart` object is k3s-only. The Phase 6 Helm chart will replace it.
- No alerting yet. Rules still evaluate and show in Prometheus; routing them is later work.
- The DCGM exporter costs 400–420 MiB of RAM per node, steady over 10 minutes: more than Prometheus
  itself (about 380 MiB). A 384 MiB limit got it OOM-killed within seconds, so the limit is 640 MiB,
  set from that measurement. The whole monitoring stack uses about 1.4 GB across the two nodes.
- llama-server exports neither time to first token nor KV-cache use; vLLM exports both. The dashboard
  shows them for vLLM only, and the week 4 benchmark measures time to first token client-side for both.
- llama-server counts only prompt tokens it computed; vLLM counts all of them and the cached ones
  separately. The dashboard subtracts vLLM's cached tokens so the two lines mean the same thing.
- The workstation's GPU isn't monitored (Windows).
- Revisit if NVIDIA's exporter stops working on Maxwell, if we move off k3s, or when alerting starts.
