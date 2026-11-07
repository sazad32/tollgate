# deploy

Kubernetes manifests for k3s (plain YAML + Kustomize, which is built into kubectl), and the Docker
Compose setup for vLLM on the workstation.

```
deploy/
  kustomization.yaml     everything Tollgate runs in k3s
  namespace.yaml         the tollgate namespace
  storage/               tollgate-local StorageClass, model-weight PVs, Prometheus PV (ADR 0006)
  llama-server/          StatefulSet (one replica per GPU node), Services, settings (ADR 0004, 0005)
  monitoring/            Prometheus + Grafana, DCGM GPU exporter, scrape targets, dashboards (ADR 0007)
  workstation/vllm/      vLLM in Docker on the workstation, outside k3s (ADR 0004)
```

## One-time prep
A local PV only mounts a directory that already exists (needs sudo on the node):
```
# on each GPU node: model weights
sudo mkdir -p /mnt/storage/tollgate/models
# on vps-node-2 only: Prometheus data, owned by the user Prometheus runs as
sudo mkdir -p /mnt/storage/tollgate/prometheus && sudo chown 1000:2000 /mnt/storage/tollgate/prometheus
```
Grafana's admin login, as a Secret that never goes in Git (PowerShell):
```powershell
$pw = -join ((48..57 + 65..90 + 97..122) | ForEach-Object { [char]$_ } | Get-Random -Count 24)
kubectl -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin --from-literal=admin-password=$pw
```
Where vLLM runs: copy `workstation/vllm/endpointslice.example.yaml` to `endpointslice.local.yaml`
(gitignored), put in the workstation's LAN address, and `kubectl apply -f` it.

## Deploy and check
```
kubectl apply -k deploy                       # create or update everything
kubectl -n tollgate get pods,pvc -o wide      # one llama-server pod per node, each claim Bound
kubectl -n tollgate logs llama-server-0 -c fetch-model   # model download / checksum
kubectl -n tollgate logs llama-server-0       # model load, VRAM use
```
First start per node: the image is about 2.5 GB and the model 1.9 GB. Later starts reuse both.

On a fresh cluster, run `kubectl apply -k deploy` **twice**. The first run creates the monitoring
`HelmChart`, and k3s installs kube-prometheus-stack in a background Job. That Job brings the CRDs the
PodMonitors and ServiceMonitors need, so they only apply on the second run.

## Look at the dashboards
```powershell
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
# open http://localhost:3000, log in as admin with:
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | ForEach-Object { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```
Our dashboards are in the **Tollgate** folder: *Model servers* and *GPUs*. Prometheus itself:
`kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090`, then
http://localhost:9090/targets.

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

- **Dashboards are generated JSON.** Edit `monitoring/dashboards/*.json` (or export from Grafana's UI)
  and re-apply. Changes made only in the UI are lost when Grafana restarts: it has no storage.
- **vLLM shows Down when the workstation is off.** That's expected, not an outage.

## Later phases
- **Phases 2–3:** Redis, Postgres (reusing `tollgate-local` with its own PV on node-2), Keycloak (with a
  realm export), the gateway and the billing consumer.
- **Phase 6:** one Helm chart for a one-command deploy.

Secrets never go in this folder. Create Kubernetes Secrets outside git (`.gitignore` blocks `**/secrets/`).
