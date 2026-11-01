# deploy

Kubernetes manifests and the Helm chart for running Tollgate on k3s.

What goes here, by phase:
- **Phase 1:** StorageClass and PVC for model weights; vLLM Deployment (one replica per GPU node) and its
  Service (week 2). kube-prometheus-stack values, the DCGM GPU exporter and Grafana dashboards (week 3).
- **Phases 2–3:** Redis, Postgres, Keycloak (with a realm export), the gateway and the billing consumer.
- **Phase 6:** one Helm chart for a one-command deploy.

Secrets never go in this folder. Create Kubernetes Secrets outside git (`.gitignore` blocks `**/secrets/`).
