# Lag Recovery Benchmark Orchestrator

Orchestrates a multi-phase consumer lag/recovery benchmark using OMB workers.
Tests how Kafka handles lagging consumers by starting/stopping consumer groups
at scheduled intervals and measuring lag accumulation and recovery time.

## Scenario

```
Phase 1 (T+0 to T+P):       All 3 consumer groups (A, B, C) consuming normally
Phase 2 (T+P to T+P+L):     Kill B and C. A continues. Lag builds on B and C.
Phase 3 (T+P+L):            Restart B. Wait for B to catch up (99% consumed).
Phase 4 (T+P+2L):           Restart C. Wait for C to catch up (99% consumed).
Cool-down (P after catch-up): All groups at steady state.
Stop:                        Stop all. Record per-group lag and recovery time.
```

Where `P` = phase duration (warm-up/cool-down), `L` = lag duration (how long groups stay stopped).

After each restart, the orchestrator waits for catch-up (`--catchup-threshold 0.99` = 99% consumed)
before proceeding. `--catchup-timeout-minutes` is a safety limit if catch-up is too slow.

Example: `--phase-duration-minutes 10 --lag-duration-minutes 60` gives B stopped for 60 min,
C stopped for 120 min. Total runtime depends on catch-up speed.

## How It Works

The orchestrator calls OMB worker REST API endpoints directly, bypassing
`Benchmark.java`. Each consumer group runs on **dedicated workers**, so
`/stop-all` on those workers stops only that group. When consumers are
recreated with the same subscription name (Kafka group ID), they resume
from committed offsets.

**Verified behavior**: Committed offsets are preserved across the
stop/reinitialize/restart cycle. Consumers do NOT re-read from the beginning.

## Prerequisites

- Python 3.9+
- Docker and Docker Compose (for local testing)
- OMB built: `mvn install -DskipTests -Dlicense.skip -pl package -am`

No Python dependencies beyond stdlib.

## Quick Start (Local Docker Compose)

### 1. Build OMB

```bash
mvn install -DskipTests -Dlicense.skip -pl package -am
```

### 2. Start infrastructure

```bash
cd deployment/docker-compose

# L1/L2: Kafka + producer + 1 consumer
docker compose up --build kafka worker-producer worker-consumer-a

# L3 (full scenario): all workers
docker compose up --build
```

### 3. Run the orchestrator

```bash
cd tools/lag-recovery-benchmark

# L1 — Smoke test (2 min): basic produce/consume cycle
python3 orchestrator.py \
  --producer-workers http://localhost:8080 \
  --consumer-a-workers http://localhost:8081 \
  --driver-config ../../deployment/docker-compose/driver.yaml \
  --topics 1 --partitions 8 \
  --target-rate-mb 1 --phase-duration-minutes 1 \
  --level 1 \
  --kafka-docker-container docker-compose-kafka-1 \
  --output l1-results.json

# L2 — Lag recovery (3 min): stop and restart one consumer group
python3 orchestrator.py \
  --producer-workers http://localhost:8080 \
  --consumer-a-workers http://localhost:8081 \
  --consumer-b-workers http://localhost:8082 \
  --driver-config ../../deployment/docker-compose/driver.yaml \
  --topics 1 --partitions 8 \
  --target-rate-mb 1 --phase-duration-minutes 1 \
  --level 2 \
  --kafka-docker-container docker-compose-kafka-1 \
  --output l2-results.json

# L3 — Full scenario with extended lag duration
python3 orchestrator.py \
  --producer-workers http://localhost:8080 \
  --consumer-a-workers http://localhost:8081 \
  --consumer-b-workers http://localhost:8082 \
  --consumer-c-workers http://localhost:8083 \
  --driver-config ../../deployment/docker-compose/driver.yaml \
  --topics 1 --partitions 8 \
  --target-rate-mb 1 --phase-duration-minutes 0.5 \
  --lag-duration-minutes 1 \
  --level 3 \
  --kafka-docker-container docker-compose-kafka-1 \
  --output l3-results.json
```

### 4. Between runs

Workers need to be in a clean state (no active driver). Either:
- Restart containers: `docker compose restart`
- Or reset via API: `for port in 8080 8081 8082 8083; do curl -s -X POST http://localhost:$port/stop-all; done`

## AWS Deployment

For production-scale benchmarks using the OMB Terraform/Ansible infrastructure.

### Setup

```bash
# 1. Provision (12 workers: 3 per role)
cd deployment/cloud-vm/aws-us-east-1
terraform apply

# 2. Deploy OMB + orchestrator + Kafka CLI to all nodes
make deploy

# 3. Upload plans (driver configs + workloads)
make upload_plans SVC_NAME=<kafka-service-name>

# 4. SSH to manager node
make connect
```

### Run from manager node

Using `--workers-file` with `--worker-split` (preferred):

```bash
python3 /opt/benchmark/orchestrator.py \
  --workers-file /opt/benchmark/workers.yaml \
  --worker-split 3,3,3,3 \
  --driver-config /etc/benchmark/drivers/<svc>-diskless-high-throughput-no-az.yaml \
  --topics 3 --partitions 192 \
  --target-rate-mb 30 --message-size 1024 \
  --phase-duration-minutes 10 \
  --lag-duration-minutes 60 \
  --catchup-timeout-minutes 120 \
  --catchup-threshold 0.99 \
  --level 3 \
  --kafka-bin /opt/kafka/bin \
  --output /tmp/results-diskless.json
```

`--kafka-bootstrap` and SSL properties are auto-extracted from the driver config.

### Worker allocation

Workers are assigned in inventory order via `--worker-split P,A,B,C`:

| Workers | Role | Notes |
|---------|------|-------|
| 1-3 | Producers | Shared, always running |
| 4-6 | Consumer Group A | Always running (baseline) |
| 7-9 | Consumer Group B | Killed at T+P, restarted at T+P+L |
| 10-12 | Consumer Group C | Killed at T+P, restarted at T+P+2L |

## CLI Reference

```
python3 orchestrator.py [OPTIONS]
```

### Worker assignment

| Option | Default | Description |
|--------|---------|-------------|
| `--workers-file` | — | Path to workers.yaml (flat list). Use with `--worker-split`. |
| `--worker-split` | — | Comma-separated split: P,A[,B[,C]]. E.g., `3,3,3,3` |
| `--producer-workers` | [] | URLs of producer workers (alternative to workers-file) |
| `--consumer-a-workers` | [] | URLs of consumer group A workers |
| `--consumer-b-workers` | [] | URLs of consumer group B workers |
| `--consumer-c-workers` | [] | URLs of consumer group C workers |

### Benchmark configuration

| Option | Default | Description |
|--------|---------|-------------|
| `--driver-config` | (required) | Path to Kafka driver YAML config |
| `--topics` | 1 | Number of topics |
| `--topic-prefix` | auto (timestamp) | Topic name prefix |
| `--partitions` | 8 | Partitions per topic |
| `--message-size` | 1024 | Message size in bytes |
| `--target-rate-mb` | 1.0 | Target producer throughput (MB/s) |

### Phase timing

| Option | Default | Description |
|--------|---------|-------------|
| `--phase-duration-minutes` | 2.0 | Warm-up and cool-down phase duration |
| `--lag-duration-minutes` | = phase duration | How long stopped groups accumulate lag |
| `--catchup-timeout-minutes` | 30 | Max time to wait for catch-up after restart |
| `--catchup-threshold` | 0.99 | Consumed fraction required (0.99 = 99% consumed before moving on) |
| `--level` | 2 | Test level: 1=smoke, 2=lag/recovery, 3=multi-group |

### Kafka lag monitoring

| Option | Default | Description |
|--------|---------|-------------|
| `--kafka-bootstrap` | (from driver config) | Override bootstrap servers for lag monitoring |
| `--kafka-bin` | "" | Path to Kafka bin dir (for kafka-consumer-groups.sh) |
| `--kafka-docker-container` | "" | Docker container for kafka-consumer-groups.sh (local testing) |

### Output

| Option | Default | Description |
|--------|---------|-------------|
| `--poll-interval` | 10 | Metrics poll interval in seconds |
| `--output` | results.json | Output file path |

## Output Format

The orchestrator writes a JSON file with:

- **config**: benchmark parameters
- **time_series**: per-poll snapshots with producer/consumer rates, lag per group
- **events**: phase transitions and offset snapshots at each transition

Events include `offsets_before_stop`, `offsets_after_stop`, `offsets_before_restart`,
`offsets_after_restart` with exact committed/log-end offsets for verification.

## Known Considerations

- **Topic naming**: Each run auto-generates a unique topic prefix (timestamp-based)
  to avoid stale data. Use `--topic-prefix` to set a fixed prefix if needed.
- **Lag monitoring**: On AWS, bootstrap and SSL properties are extracted from
  the driver config automatically. Use `--kafka-bin /opt/kafka/bin` to point
  to the Kafka CLI tools. For local Docker testing, use `--kafka-docker-container`.
