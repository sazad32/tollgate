# 0006 — Model weights: one copy per node on local volumes

- Status: accepted
- Date: 2026-11-03

## Context
`llama-server` runs one replica per GPU node (ADR 0004), and each replica needs the model file (1.9 GB,
ADR 0005) on disk. node-2 has a 458 GB data disk at `/mnt/storage`; node-1 has only its root disk
(80 GB free). There is no shared storage between the nodes.

Two Kubernetes facts shape the options:
- A local PersistentVolume can only be mounted on the node it lives on.
- Every replica of a Deployment uses the same volume claim, so a Deployment can't give each replica
  its own node-local volume.

## Options considered
1. **hostPath in a Deployment or DaemonSet.** Simplest. No PV, no claim and no scheduler awareness: a
   pod can land on a node without the weights, and it's not the storage model we'll need for Postgres.
2. **One copy on node-2, shared over NFS.** One download, one copy. Needs an NFS server on node-2 and
   client packages on node-1. node-1's replica then depends on node-2, which defeats having two
   replicas.
3. **k3s's local-path provisioner pointed at `/mnt/storage`.** Dynamic volumes, no hand-written PVs.
   But k3s owns that provisioner's config and rewrites it on restart; changing its path means a
   server flag and a k3s restart on the control-plane node.
4. **Static local PVs, one per node, claimed by a StatefulSet.** A `tollgate-local` StorageClass with no
   provisioner and `WaitForFirstConsumer`, one PV per GPU node, and `volumeClaimTemplates` so each
   replica gets its own claim. The scheduler binds each claim to the PV on the node it picks.

## Decision
Option 4. Each GPU node keeps its own copy of the weights at `/mnt/storage/tollgate/models`, and
`llama-server` is a StatefulSet. An init container downloads the model from a pinned Hugging Face
commit and checks its SHA-256 on every start, skipping the download when the file is already there.

## Consequences
- Each node downloads the model once. Weights are small and can be downloaded again, so two copies
  cost less than shared storage would.
- A replica starts without the other node, so losing a node costs half the capacity, not all of it.
- StatefulSet instead of Deployment. Side benefits: stable pod names (`llama-server-0`, `-1`), a DNS name
  per pod through the headless Service (per-pod metrics in week 3, per-backend routing in week 8), and
  rolling updates that replace a pod without adding a surge pod that could never get a GPU.
- PVs use `Retain`: the weights survive deleting the claims, but a Released PV must have its claim
  cleared by hand before it binds again (see `deploy/README.md`).
- One-time manual step: `sudo mkdir` of the PV path on each node. Adding a GPU node means one more PV,
  one more `mkdir` and one more replica.
- node-1's copy sits on its root disk. Fine for weights, but durable data (Postgres, Phase 3) goes on
  node-2's data disk, with its own PV in the same StorageClass.
- Local volumes don't enforce the size on the PV; it's only used to match claims to PVs.
- Revisit if the cluster gets shared storage or more than a few nodes, where hand-written PVs stop
  scaling.
