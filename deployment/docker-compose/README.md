# Local environment with Docker Compose

This environment runs one Kafka broker (KRaft mode) and four OMB workers on your machine. Use it to test a workload or a tool before you deploy to a cloud.

|       Service       | Port on host |
|---------------------|--------------|
| `kafka`             | 9092         |
| `worker-producer`   | 8080         |
| `worker-consumer-a` | 8081         |
| `worker-consumer-b` | 8082         |
| `worker-consumer-c` | 8083         |

## Start the environment

1. Build the distribution tarball. Run this command in the root of the repository.

   ```shell
   make build
   ```
2. Build the images and start the containers.

   ```shell
   cd deployment/docker-compose
   docker compose up -d --build
   ```

## Run a workload

Run the benchmark client in the root of the repository. The client sends `driver.yaml` to the workers. The workers resolve `kafka:9092` inside the Compose network.

```shell
bin/benchmark --drivers deployment/docker-compose/driver.yaml \
  --workers http://localhost:8080,http://localhost:8081 \
  workloads/1-topic-1-partition-1kb.yaml
```

This workload runs for 15 minutes. Use another file in `workloads/` to change the load.

## Stop the environment

Run this command in the root of the repository.

```shell
docker compose -f deployment/docker-compose/docker-compose.yaml down -v
```

