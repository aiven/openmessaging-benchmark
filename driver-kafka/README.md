# Apache Kafka benchmarks

For instructions on running the OpenMessaging benchmarks, see the [root README](../README.md).

NOTE: This is a slightly modified version with two key differences:

- workloads have extra arguments for sending randomized payloads rather than the same payload over and over.
- there is a new argument that converts all output result json files into a single csv.

TODO: Document these changes.

## Features

### Zone-aware workers

To pass a zone/rack ID (e.g. cloud region availability zone name) to the Kafka clients (producer, consumer) client-id configuration, use the system property `zone.id`, and use the template `{zone.id}` on the `client.id` or `client.rack` configs, either on the `commonConfig`, `producerConfig`, or `consumerConfig` Driver values.

When running workers, pass the `zone.id`:

```bash
export JVM_OPTS=-Dzone.id=az0
/opt/benchmark/bin/benchmark-worker
```

Then pass the `client.id` template:

```yaml
commonConfig: |
  bootstrap.servers=localhost:9092
  client.id=omb-client_az={zone.id}
  client.rack={zone.id}
```

This generates producer and consumer `client.id=omb-client_az=value` and `client.rack=omb-client_az=value`

```yaml
producerConfig: |
  client.id=omb-producer_az={zone.id}
  client.rack={zone.id}
consumerConfig: |
  auto.offset.reset=earliest
  client.id=omb-consumer_az={zone.id}
  client.rack={zone.id}
```

This generates producer `client.id=omb-producer_az=value` and `client.rack=value`; consumer `client.id=omb-consumer_az=value` and `client.rack=value`

### Skip the existing backlog

To measure only the messages that producers send in the run, set `resetToLatest: true` in the driver file.

```yaml
resetToLatest: true
```

Each consumer waits for its partition assignment. Then it moves each assigned partition to the end offset and commits that offset to its consumer group. A later rebalance does not move the position back.

The option has four limits:

- Each consumer needs at least one partition. If a consumer has no partition after 2 minutes, `createConsumer` fails. Do not create more consumers than partitions. The worker tries all its consumers before it reports the failure, so each surplus consumer adds 2 minutes.
- The option changes the committed offsets of the consumer group.
- A worker creates its consumers one after the other, and each consumer waits for its assignment. A new consumer group needs about 3 seconds with the broker default `group.initial.rebalance.delay.ms`. A worker request times out after 10 minutes, so a worker with a few hundred consumer groups can time out.
- The option is tested with the classic consumer group protocol only. The `consumer` protocol (KIP-848) is not tested.

