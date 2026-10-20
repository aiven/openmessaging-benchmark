# Aiven services for benchmarks

Terraform for the Aiven services that a benchmark runs against: Kafka, Thanos and Grafana.
The benchmark workers are not part of this module. Run them where you like, for example in the same cloud region as the services.

## Usage

1. Copy `envs/example.tfvars` to `envs/<name>.tfvars` and set the project, cloud, and plans.
2. Export the token: `export AIVEN_TOKEN=<token>`.
3. Run `make plan ENV=<name>`, then `make apply ENV=<name>`.

Each `ENV` has its own Terraform workspace and local state.
State and `*.tfvars` files can hold credentials. Git ignores them. Do not commit them.

## What it creates

- `<prefix>-kafka`: Aiven for Apache Kafka. Optional Inkless, diskless, and tiered storage settings.
- `<prefix>-thanos`: Aiven for Thanos. Receives the Kafka `metrics` integration.
- `<prefix>-grafana`: Aiven for Grafana. Reads Thanos through a `datasource` integration and a `dashboard` integration. The `dashboard` integration adds the built-in Aiven dashboard of the Kafka service. [`deployment/cloud-vm`](../cloud-vm/README.md#send-the-metrics-to-the-aiven-thanos) loads the OMB dashboards.

## Custom API URL

The provider has no argument for the API URL. Both of its client libraries read `AIVEN_WEB_URL`.
Set it, with a token that is valid on that endpoint, before you run `make`:

```
export AIVEN_WEB_URL=https://<api-host>:443
export AIVEN_TOKEN=<token>
```

Do not add a trailing `/` to the URL.
A token set in the `aiven_api_token` variable overrides `AIVEN_TOKEN`.

## Outputs

The outputs of the Kafka service are the connection settings for the benchmark drivers. `thanos_remote_write_uri`, `thanos_username` and `thanos_password` are the settings for Prometheus `remote_write`. `grafana_service_uri`, `grafana_username` and `grafana_password` are the Grafana URL and login. `cloud_name` is the cloud and region of the services. Use `terraform output -raw <name>` to read a value.

