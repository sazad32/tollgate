# deploy

Kubernetes manifests for k3s (plain YAML + Kustomize, which is built into kubectl), and the Docker
Compose setup for vLLM on the workstation.

```
deploy/
  kustomization.yaml     everything Tollgate runs in k3s
  namespace.yaml         the tollgate namespace
  storage/               tollgate-local StorageClass + one model-weights PV per GPU node (ADR 0006)
  llama-server/          StatefulSet (one replica per GPU node), Services, settings (ADR 0004, 0005)
  workstation/vllm/      vLLM in Docker on the workstation, outside k3s (ADR 0004)
```

## One-time node prep
A local PV only mounts a directory that already exists. On each GPU node (needs sudo):
```
sudo mkdir -p /mnt/storage/tollgate/models
```

## Deploy and check
```
kubectl apply -k deploy                       # create or update everything
kubectl -n tollgate get pods,pvc -o wide      # one llama-server pod per node, each claim Bound
kubectl -n tollgate logs llama-server-0 -c fetch-model   # model download / checksum
kubectl -n tollgate logs llama-server-0       # model load, VRAM use
```
First start per node: the image is about 2.5 GB and the model 1.9 GB. Later starts reuse both.

## Try it
```
kubectl -n tollgate port-forward svc/llama-server 8080:8080
curl http://localhost:8080/v1/models
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "smollm3-3b", "messages": [{"role": "user", "content": "Say hi in five words."}]}'
```
A port-forward to a Service sticks to one pod. To reach a specific replica from inside the cluster, use
its headless DNS name: `llama-server-0.llama-server-headless.tollgate.svc:8080`.

## Change the model or its settings
Edit `llama-server/llama-server.env` (URL, SHA-256, file name, alias, context) and run
`kubectl apply -k deploy`. The ConfigMap name carries a content hash, so the pods roll one at a time.
Keep the alias and context in step with `workstation/vllm/compose.yaml`.

## Gotchas
- **The PVs use `Retain`.** Deleting a claim keeps the weights but leaves the PV `Released`, and a
  Released PV won't bind again until you clear its claim:
  `kubectl patch pv tollgate-models-vps-node-1 -p '{"spec":{"claimRef":null}}'`.
- **One GPU per node.** A third replica, or a second GPU workload on a node, stays `Pending`.
- **First request after a fresh start is slower** on the Maxwell nodes: the driver compiles GPU kernels
  from PTX on first use. The compiled cache is kept on the model volume (`CUDA_CACHE_PATH`).

## Later phases
- **Week 3:** kube-prometheus-stack values, GPU metrics and Grafana dashboards.
- **Phases 2–3:** Redis, Postgres (reusing `tollgate-local` with its own PV on node-2), Keycloak (with a
  realm export), the gateway and the billing consumer.
- **Phase 6:** one Helm chart for a one-command deploy.

Secrets never go in this folder. Create Kubernetes Secrets outside git (`.gitignore` blocks `**/secrets/`).
