# Lag Recovery Benchmark Orchestrator

Orchestrates a multi-phase consumer lag/recovery benchmark using OMB workers.
Tests how Kafka handles lagging consumers by starting/stopping consumer groups
at scheduled intervals and measuring lag accumulation and recovery time.

## Scenario

```
Phase 1 (T+0 to T+P):     All 3 consumer groups (A, B, C) consuming normally
Phase 2 (T+P to T+2P):    Kill B and C. A continues. Lag builds on B and C.
Phase 3 (T+2P to T+3P):   Restart B. A stays current, B catches up. C still stopped.
Phase 4 (T+3P to T+4P):   Restart C. A and B stay current, C catches up.
Stop (T+4P):               Stop all. Record per-group lag and recovery time.
```

Where `P` = phase duration (configurable, default 2 minutes for local, 10 minutes for production).

## How It Works

The orchestrator calls OMB worker REST API endpoints directly, bypassing
`Benchmark.java`. Each consumer group runs on **dedicated workers**, so
`/stop-all` on those workers stops only that group. When consumers are
recreated with the same subscription name (Kafka group ID), they resume
from committed offsets.

**Verified behavior**: Committed offsets are preserved across the
stop/reinitialize/restart cycle. Consumers do NOT re-read from the beginning.

## Prerequisites

- Python 3.10+
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
docker compose up --build kafka worker-producer worker-consumer-a worker-consumer-b worker-consumer-c
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
  --level 1 --kafka-bootstrap kafka:9092 \
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
  --level 2 --kafka-bootstrap kafka:9092 \
  --kafka-docker-container docker-compose-kafka-1 \
  --output l2-results.json

# L3 — Full scenario (4 min): 3 groups with staggered restart
python3 orchestrator.py \
  --producer-workers http://localhost:8080 \
  --consumer-a-workers http://localhost:8081 \
  --consumer-b-workers http://localhost:8082 \
  --consumer-c-workers http://localhost:8083 \
  --driver-config ../../deployment/docker-compose/driver.yaml \
  --topics 1 --partitions 8 \
  --target-rate-mb 1 --phase-duration-minutes 1 \
  --level 3 --kafka-bootstrap kafka:9092 \
  --kafka-docker-container docker-compose-kafka-1 \
  --output l3-results.json
```

### 4. Between runs

Workers need to be in a clean state (no active driver). Either:
- Restart containers: `docker compose restart`
- Or reset via API: `for port in 8080 8081 8082 8083; do curl -s -X POST http://localhost:$port/stop-all; done`

## AWS Deployment (L4/L5)

For production-scale benchmarks using existing OMB Terraform/Ansible infrastructure.

### Target configuration

```bash
python3 orchestrator.py \
  --producer-workers http://worker1:8080 http://worker2:8080 \
  --consumer-a-workers http://worker3:8080 http://worker4:8080 \
  --consumer-b-workers http://worker5:8080 http://worker6:8080 \
  --consumer-c-workers http://worker7:8080 http://worker8:8080 \
  --driver-config /etc/benchmark/drivers/lag-recovery.yaml \
  --topics 3 --partitions 192 \
  --target-rate-mb 30 --message-size 1024 \
  --phase-duration-minutes 10 \
  --level 3 \
  --kafka-bootstrap <kafka-bootstrap>:9092 \
  --poll-interval 10 \
  --output results.json
```

This produces:
- 30 MB/s IN (split across 2 producer workers)
- 60 MB/s OUT when all 3 groups active (each reads 30 MB/s)
- 10-minute phases, 40 minutes total

### Worker allocation

| Workers | Role | Notes |
|---------|------|-------|
| 1-2 | Producers | Shared, always running |
| 3-4 | Consumer Group A | Always running (baseline) |
| 5-6 | Consumer Group B | Killed at T+10M, restarted at T+20M |
| 7-8 | Consumer Group C | Killed at T+10M, restarted at T+30M |

## CLI Reference

```
python3 orchestrator.py [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--producer-workers` | (required) | URLs of producer OMB workers |
| `--consumer-a-workers` | [] | URLs of consumer group A workers |
| `--consumer-b-workers` | [] | URLs of consumer group B workers |
| `--consumer-c-workers` | [] | URLs of consumer group C workers |
| `--driver-config` | (required) | Path to Kafka driver YAML config |
| `--topics` | 1 | Number of topics |
| `--topic-prefix` | auto (timestamp) | Topic name prefix. Auto-generated per run to avoid stale data. |
| `--partitions` | 8 | Partitions per topic |
| `--message-size` | 1024 | Message size in bytes |
| `--target-rate-mb` | 1.0 | Target producer throughput (MB/s) |
| `--phase-duration-minutes` | 2.0 | Duration of each phase |
| `--level` | 2 | Test level: 1=smoke, 2=lag/recovery, 3=multi-group |
| `--kafka-bootstrap` | localhost:9092 | Kafka bootstrap servers (for lag monitoring) |
| `--kafka-bin` | "" | Path to Kafka bin dir (host mode) |
| `--kafka-docker-container` | "" | Docker container for kafka-consumer-groups.sh |
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
- **Rate display**: The console display divides period totals by poll_interval (10s),
  but actual poll periods may vary (10-14s). The underlying rate limiter is accurate.
- **Lag monitoring**: On AWS, use `--kafka-bin` pointing to the Kafka installation
  on the worker VMs, or run the orchestrator from a machine with Kafka CLI tools.
  For local Docker testing, use `--kafka-docker-container`.
