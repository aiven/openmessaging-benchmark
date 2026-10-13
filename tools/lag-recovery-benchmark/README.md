# Lag Recovery Benchmark Orchestrator

The orchestrator runs a multi-phase consumer lag and recovery benchmark with OMB workers.
It starts and stops consumer groups on a schedule. It measures how lag builds and how long each group needs to catch up.

Status: experimental. The tool works for the levels below, but its measurements have limits. Read [Limitations](#limitations) before you use the results.

## Scenarios

Each level uses more consumer groups. `P` is the phase duration. `L` is the lag duration, the time a stopped group accumulates lag.

| Level |   Groups   |                                            Scenario                                             |
|-------|------------|-------------------------------------------------------------------------------------------------|
| 1     | none or A  | Smoke test: produce for `2P`. Group A, if you give workers for it, consumes. No consumer stops. |
| 2     | A, B       | Warm up for `P`. Stop B. Wait `L`. Restart B. Wait for catch-up. Cool down `P`.                 |
| 3     | A, B, C    | Stop B and C. Restart B after `L`. Restart C after `2L`, or later if B is still catching up.    |
| 4     | A, B, C, D | A, B and C read at the head. D starts at `P + L` from offset 0 and reads the older data first.  |
| 5     | A, B       | Seeded topic (`--reuse-topic`). Reader A starts from offset 0. B starts only if A catches up.   |

Level 2 needs workers for B. Level 3 needs workers for B and C. Level 4 needs workers for B, C and D. Levels 4 and 5, and every run with `--reuse-topic`, start new consumer groups that must read from the beginning, so the driver file needs `auto.offset.reset=earliest` in `consumerConfig`. The orchestrator checks the options and rejects values such as a poll interval of 0, a target rate below 1 message per second (the limit of the stall detector), or a worker URL that serves two roles.

Level 3 example: `--phase-duration-minutes 10 --lag-duration-minutes 60` stops B for 60 minutes and C for 120 minutes.

After a restart, the orchestrator waits until the group consumed `--catchup-threshold` of the log (default 0.99) on all partitions of the topics. `--catchup-timeout-minutes` limits this wait. If a group does not catch up in time, the run continues to its end, writes `status: timeout` and exits with code 3.

## How It Works

The orchestrator calls the REST API of the OMB workers. It does not use `Benchmark.java`.
Each consumer group runs on dedicated workers. A `/stop-all` call on these workers stops only that group.
Each run adds a random run ID to the Kafka group ID (for example `group-B-1a2b3c4d`), so the offsets that a group kept from earlier runs do not count. Within a run, the ID stays the same. When the orchestrator creates the consumers again, they resume from the committed offsets.
The orchestrator reads group offsets and lag with `kafka-consumer-groups.sh`. Before it calls a worker, it runs `kafka-consumer-groups.sh --list` once. If this call fails, the run stops with exit code 1.

The orchestrator creates the topics with explicit names (`topicNames`), so the workers must run a build that has this input. If the workers create other topic names, the run stops with exit code 1.

A stall detector stops the run when the producer rate stays near zero for several polls, for example when a worker died.

## Prerequisites

- Python 3.9 or later. The orchestrator uses only the standard library.
- Workers that you can reach over HTTP.
- `kafka-consumer-groups.sh` on the `PATH` of the host that runs the orchestrator, in `--kafka-bin`, or in a Docker container.

## Quick Start With Docker Compose

1. Start the environment in [`deployment/docker-compose`](../../deployment/docker-compose/README.md). Start all workers for levels 3 and 4.

2. Run the orchestrator in this directory. This example is a level 2 run of about 3 minutes.

   ```shell
   cd tools/lag-recovery-benchmark
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
   ```
3. Between runs, reset the workers. Restart the containers, or call `/stop-all` on each worker.

   ```shell
   for port in 8080 8081 8082 8083; do curl -s -X POST http://localhost:$port/stop-all; done
   ```

For level 3, add `--consumer-c-workers http://localhost:8083`.

## Remote Workers

Run the orchestrator on a host that reaches the workers. Pass the workers in one of two ways:

- `--producer-workers` and `--consumer-{a,b,c,d}-workers` with the URLs of each role.
- `--workers-file` with a YAML file and `--worker-split` with the number of workers per role. Use the two options together. The file has a `workers:` key with a list of URLs, one unquoted `- http...` item per line. The split lists the roles in the order producers, A, B, C, D. For example, `--worker-split 3,3,3,3` gives 3 workers to each of the producers, A, B and C.

Set `--kafka-bin` to the directory with the Kafka CLI tools. The orchestrator reads the bootstrap servers and the SSL properties from the driver config. `--kafka-bootstrap` overrides the bootstrap servers.

## CLI Reference

```
python3 orchestrator.py [OPTIONS]
```

### Worker assignment

|         Option         | Default |                             Description                              |
|------------------------|---------|----------------------------------------------------------------------|
| `--workers-file`       |         | File with the worker URLs. Use with `--worker-split`.                |
| `--worker-split`       |         | Workers per role: `P,A[,B[,C[,D]]]`. Example: `3,3,3,3`.             |
| `--producer-workers`   | []      | URLs of the producer workers.                                        |
| `--consumer-a-workers` | []      | URLs of the workers of consumer group A.                             |
| `--consumer-b-workers` | []      | URLs of the workers of consumer group B.                             |
| `--consumer-c-workers` | []      | URLs of the workers of consumer group C.                             |
| `--consumer-d-workers` | []      | URLs of the workers of group D (level 4, starts late from offset 0). |

### Benchmark configuration

|       Option       |        Default        |                                                                   Description                                                                   |
|--------------------|-----------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `--driver-config`  | (required)            | Path to the Kafka driver YAML file.                                                                                                             |
| `--topics`         | 1                     | Number of topics.                                                                                                                               |
| `--topic-prefix`   | auto (timestamp + ID) | Topic name prefix.                                                                                                                              |
| `--partitions`     | 8                     | Partitions per topic.                                                                                                                           |
| `--message-size`   | 1024                  | Message size in bytes.                                                                                                                          |
| `--target-rate-mb` | 1.0                   | Target producer throughput in MB/s.                                                                                                             |
| `--reuse-topic`    | off                   | Attach to a seeded topic. Requires `--topic-prefix`. The groups of the run are new, so they read from the earliest offset. Level 5 requires it. |

### Phase timing

|           Option            |    Default     |                  Description                  |
|-----------------------------|----------------|-----------------------------------------------|
| `--phase-duration-minutes`  | 2.0            | Duration of the warm-up and cool-down phases. |
| `--lag-duration-minutes`    | phase duration | Time that stopped groups accumulate lag.      |
| `--catchup-timeout-minutes` | 30             | Maximum wait for catch-up after a restart.    |
| `--catchup-threshold`       | 0.99           | Consumed fraction that counts as caught up.   |
| `--level`                   | 2              | Scenario level, 1 to 5.                       |

### Kafka lag monitoring

|           Option           |     Default      |                                                        Description                                                        |
|----------------------------|------------------|---------------------------------------------------------------------------------------------------------------------------|
| `--kafka-bootstrap`        | from driver file | Bootstrap servers for the lag queries. Without the option, the address in the driver file applies, then `localhost:9092`. |
| `--kafka-bin`              |                  | Directory of `kafka-consumer-groups.sh`.                                                                                  |
| `--kafka-docker-container` |                  | Docker container that runs `kafka-consumer-groups.sh`.                                                                    |

### Output

|      Option       |   Default    |          Description          |
|-------------------|--------------|-------------------------------|
| `--poll-interval` | 10           | Seconds between metric polls. |
| `--output`        | results.json | Path of the result file.      |

## Output Format

The orchestrator writes one JSON file with these keys:

- `config`: the benchmark parameters.
- `time_series`: one entry per poll, with producer rates, consumer rates and lag per group.
- `events`: phase changes, with the committed and log-end offsets at each change. Each catch-up wait adds a `catchup` event with `groups`, `caught_up` and `waited_s`.
- `status`: `completed`, `timeout` or `aborted`.
  - `timeout`: a group did not catch up within `--catchup-timeout-minutes`. The run reached its end. The `catchup` events show the group.
  - `aborted`: a stall, a failed phase or an interrupt ended the run. An aborted run also has `error`, and the file holds the time series and the events up to that point.

The exit code is 0 for `completed`, 1 for a failure or an invalid option, 2 for a stalled workload and 3 for `timeout`.

The events `offsets_before_stop`, `offsets_after_stop`, `offsets_before_restart` and `offsets_after_restart` hold the totals and the offsets of each partition (`partitions`). Use them to check that a restarted group resumes from its committed offsets.

## Tests

Run the unit tests in this directory:

```shell
python3 -m unittest -v test_orchestrator
```

## Limitations

### Measurement

- The catch-up fraction is `1 - lag / log-end offset` over committed offsets, not the consumer position. The consumer commits after each poll, so the fraction trails the real progress by one commit.
- The fraction counts offsets from 0, so it assumes that the log of a topic starts at offset 0. A seeded topic (`--reuse-topic`) whose old data retention has deleted gives a fraction that is too high. Use a seeded topic that retention has not trimmed.
- The threshold is relative. On a large seeded log, 1% can be many messages, so a group can count as caught up before it reaches the head. There is no absolute lag limit.
- A partition without a committed offset counts its whole log as lag. If `--describe` lists fewer partitions than the topics have, for example during a rebalance, the group is not caught up.
- The catch-up time has the resolution of one poll: `--poll-interval` plus one `kafka-consumer-groups.sh` call (one JVM start) per consumer group. With 4 groups, a poll of 10 s takes about 20 s. `waited_s` starts after the consumers are created. The timeout has the same resolution: a group that catches up in the poll that crosses `--catchup-timeout-minutes` counts as caught up, and `waited_s` shows the real wait.
- Level 3 measures the outage of C from the end of the warm-up. The stop of B and C takes a few seconds, so the outage of C is shorter than `2L` by that time. The events show the actual stop and restart times.
- Throughput in MB/s is the message rate times `--message-size`, because the worker counters have no byte count. With `--reuse-topic`, set `--message-size` to the message size of the seeded data, or use only the msg/s values.
- A rate without a baseline is `null`: the first poll, the first poll after a restart, and the poll after a failed poll.
- The workers reset their counters when a group stops, so `total_received` starts at 0 after each restart.

### Run control

- Workers must have no active driver at the start of a run. Restart them or call `/stop-all`.
- A worker that hangs blocks the run for up to 600 s per HTTP call. The stall detector does not run during that call.
- A failure before the scenario starts writes no result file, and an old file at `--output` stays. Use the exit code.
- A failed final `stop_all` gives `aborted` (exit code 1), also when a group timed out before.
- Docker mode (`--kafka-docker-container`) passes no `--command-config`, so use host mode for SSL or SASL clusters. Host mode takes only the `security.*`, `ssl.*` and `sasl.*` keys of `commonConfig`.
- The orchestrator reads the driver file line by line, not as YAML. Only `key=value` lines under `commonConfig: |` and `consumerConfig: |` count.
- If the process is killed, the command-config file with the connection properties stays in the temporary directory. Its mode is 0600.

### Topics

- Each run creates topics with a timestamp and a run ID in the name, so old data does not affect the run. Set `--topic-prefix` to use a fixed prefix.
- With `--reuse-topic`, the groups read the old messages of the topic from the beginning. The worker counters include these messages.
- With `--reuse-topic`, all topics must exist and have the same partition count. The orchestrator checks this before it calls a worker.
- Consumer groups of earlier runs stay in Kafka. The orchestrator does not delete them.

