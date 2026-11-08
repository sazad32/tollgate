#!/bin/bash
# Runs once, when Postgres starts with an empty data directory (the image's /docker-entrypoint-initdb.d).
# One database and one login per application, so neither can read the other's tables (ADR 0011).
# Passwords come from the postgres-credentials Secret; they're generated alphanumeric, so they're safe
# to place inside SQL string literals.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
	CREATE ROLE tollgate LOGIN PASSWORD '${TOLLGATE_DB_PASSWORD}';
	CREATE DATABASE tollgate OWNER tollgate;
	CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
	CREATE DATABASE keycloak OWNER keycloak;
SQL
