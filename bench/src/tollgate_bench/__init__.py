"""Tollgate benchmark: time to first token and tokens/s per model server (ADR 0008).

Runs in two places: on the workstation (tests, `job` and `report`) and inside the cluster as a Job,
where the code is mounted from a ConfigMap instead of installed. So nothing here may depend on
installed package metadata.
"""
