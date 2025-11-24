# Aiven's Fork of the OpenMessaging Benchmark Framework

[![Build](https://github.com/aiven/openmessaging-benchmark/actions/workflows/pr-build-and-test.yml/badge.svg)](https://github.com/aiven/openmessaging-benchmark/actions/workflows/pr-build-and-test.yml)
[![License](https://img.shields.io/badge/license-Apache%202-4EB1BA.svg)](https://www.apache.org/licenses/LICENSE-2.0.html)

This repository houses user-friendly, cloud-ready benchmarking suites for the following messaging platforms:

* [Apache ActiveMQ Artemis](https://activemq.apache.org/components/artemis/)
* [Apache Bookkeeper](https://bookkeeper.apache.org)
* [Apache Kafka](https://kafka.apache.org)
* [Apache Pulsar](https://pulsar.apache.org)
* [Apache RocketMQ](https://rocketmq.apache.org)
* Generic [JMS](https://javaee.github.io/jms-spec/)
* [KoP (Kafka-on-Pulsar)](https://github.com/streamnative/kop)
* [NATS JetStream](https://docs.nats.io/nats-concepts/jetstream)
* [NATS Streaming (STAN)](https://docs.nats.io/legacy/stan/intro)
* [NSQ](https://nsq.io)
* [Pravega](https://pravega.io/)
* [RabbitMQ](https://www.rabbitmq.com/)
* [Redis](https://redis.com/)

However, this fork is primarily focused on updating Kafka benchmarking.

## How to use

### Components

The OpenMessaging Benchmark Framework contains two components - the driver, and the workers.

* Driver - The main “driver” is responsible to assign the tasks, creating the benchmark topic, creating the consumers & producers, etc. The benchmark executor.
* Worker - A benchmark worker that listens to tasks to perform them. A worker ensemble communicates over HTTP (defaults to port 8080).

### Configuration

#### Drivers

The drivers contain all the configuration specific to the messaging system you want to benchmark; including topic, producer and consumer configuration.

```yaml
name: kafka-production
driverClass: io.openmessaging.benchmark.driver.kafka.KafkaBenchmarkDriver

# Kafka topic-specific configuration
replicationFactor: 3
topicConfig: |
  min.insync.replicas=2

# Kafka client-specific configuration
commonConfig: |
  bootstrap.servers=localhost:9092
  client.id=benchmark-client
  client.rack={zone.id}  # optional, used for rack-aware partition assignment

producerConfig: |
  acks=all
  linger.ms=100
  batch.size=1048576
  max.request.size=4194304

consumerConfig: |
  auto.offset.reset=earliest
  enable.auto.commit=false
  max.partition.fetch.bytes=10485760
```

Here is where producers and consumers can be configured for low-latency or high-throughput workloads.

##### Zone Awareness

The `zone.id` variable can be passed via JVM system properties to enable zone-aware partition assignment.
For example, if you have 3 availability zones, you can start your workers with:

```yaml
JVM_OPTS="-Dzone.id=us-east-1a" bin/benchmark-worker --port 8080 --stats-port 9091
```

This will set the `zone.id` variable for each worker, which can then be used in the driver configuration.

```yaml
client.rack={zone.id}
client.id=benchmark-client,diskless_az={zone.id}
```

#### Workloads

This is where you define the actual benchmark workload - number of topics, partitions, producers, consumers, message size, throughput, etc.

```yaml
name: 1-topic-576-partitions-1kb-144-producers

# Durations
warmupDurationMinutes: 5
testDurationMinutes: 60

# Topic partition topology
topics: 1
partitionsPerTopic: 576

# Clients per topic
producersPerTopic: 144

subscriptionsPerTopic: 3
consumerPerSubscription: 144

# Throughput
producerRate: 1048576

messageSize: 1024
useRandomizedPayloads: true
randomBytesRatio: 0.8
randomizedPayloadPoolSize: 1000

# Backlog
consumerBacklogSizeGB: 0
```

Some considerations when defining workloads:
- Producers and Consumer instances are defined per topic.
- Throughput is evenly divided among all producers.
- When producerRate is set to 0, producers will send messages as fast as possible.
- Message content can be fixed (provided values on a `payloadFile`) or randomized.
- Backlog size is defined in GB per consumer group. e.g., with 3 subscriptions and 10 GB backlog, each subscription will have ~3.33 GB backlog.
- Backlog adds a couple of extra phases to the benchmark - after warmup, it builds the backlog, and then drains it. After that, the actual test starts.

### Basic commands

#### Driver

```yaml
bin/benchmark \
  --drivers driver-kafka/kafka-exactly-once.yaml \
  --workers 1.2.3.4:8080,4.5.6.7:8080 \ # or -w 1.2.3.4:8080,4.5.6.7:8080
  workloads/1-topic-16-partitions-1kb.yaml
```

|         Flag         | Description                                                       | Default |
|----------------------|:------------------------------------------------------------------|---------|
| -c / --csv           | Print results from this directory to a CSV file.                  | N/A     |
| -d / --drivers       | Drivers list. eg.: pulsar/pulsar.yaml,kafka/kafka.yaml            | N/A     |
| -x / --extra         | Allocate extra consumer workers when your backlog builds.         | false   |
| -w / --workers       | List of worker nodes. eg: http://1.2.3.4:8080,http://4.5.6.7:8080 | N/A     |
| -wf / --workers-file | Path to a YAML file containing the list of workers addresses.     | N/A     |
| -h / --help          | Print help message                                                | false   |

#### Worker

```yaml
bin/benchmark-worker --port 8080 --stats-port 9091
```

| Flag               | Description              | Default |
|:-------------------|:-------------------------|--------:|
| -p / --port        | HTTP port to listen to.  |    8080 |
| -sp / --stats-port | Stats port to listen to. |    8081 |
| -h / --help        | Print help message       |   false |

## Build

Requirements:

* JDK 17
* Maven 3.8.6+

Common build actions:

|             Action              |                 Command                  |
|---------------------------------|------------------------------------------|
| Full build and test             | `mvn clean verify`                       |
| Skip tests                      | `mvn clean verify -DskipTests`           |
| Skip Jacoco test coverage check | `mvn clean verify -Djacoco.skip`         |
| Skip Checkstyle standards check | `mvn clean verify -Dcheckstyle.skip`     |
| Skip Spotless formatting check  | `mvn clean verify -Dspotless.check.skip` |
| Format code                     | `mvn spotless:apply`                     |
| Generate license headers        | `mvn license:format`                     |

## Docker

This fork is used to update the outdated offical docker hub image openmessaging/openmessaging-benchmark with sapmachine:17.
Find more in [./docker/README.md](./docker/README.md).

## License

Licensed under the Apache License, Version 2.0: http://www.apache.org/licenses/LICENSE-2.0
