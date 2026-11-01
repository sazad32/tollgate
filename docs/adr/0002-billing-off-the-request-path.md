# 0002 — Billing runs off the request path via Redis Streams

- Status: accepted
- Date: 2026-10-31

## Context
If the gateway writes to the billing database synchronously, a Postgres outage or slow query takes the API down.

## Options considered
1. Synchronous write to Postgres per request — simple, couples availability
2. Redis Streams + async consumer — durable, ordered, one less system to run
3. Kafka / RabbitMQ — robust, but heavy for a 2-node home cluster

## Decision
The gateway emits one usage event per request to Redis Streams. A separate billing consumer writes the ledger.
Every event carries an idempotency key; the ledger enforces uniqueness on it.

## Consequences
Billing is eventually consistent (seconds behind). Reconciliation jobs are needed. Redis persistence (AOF)
must be enabled so events survive a restart.
