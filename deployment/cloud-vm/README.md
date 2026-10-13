# OMB deployment on Cloud VMs

Deployment scripts to provision and setup OMB workers in cloud providers using Terraform and Ansible

Features:

- Provision Cloud VMs
- Install and setup OMB binaries
- Monitoring stack for OMB workers and nodes

Requirements:

- Terraform
- Ansible
- Aiven client
- rsync, on your machine and on the nodes (the deploy playbook installs it on the nodes)

Supported VMs:

## How to use

Each environment is a variables file, `envs/<name>.tfvars`, with its own Terraform workspace and Ansible inventory.
Copy [`envs/example.tfvars`](./envs/example.tfvars) to `envs/<name>.tfvars` and set the values for your account.
Git ignores everything in `envs/` except the example, so account details do not get committed.
See the Cloud provider module guide for the variables:

- [AWS EC2](./terraform/aws/README.md)

> WARNING: This workflow stores Terraform state in a different place than the previous `TF_DIR` workflow. Do not provision a new environment over a running one. Destroy each existing environment with the previous Makefile first: `make TF_DIR=<dir> destroy`.

Setup the credentials depending on the Cloud provider, and then run:

```shell
make provision ENV=<name>
```

If it finishes successfully, an Ansible inventory file will be generated at `envs/<name>/hosts.yaml`.
Every `make` target except `ssh_key`, `build`, `rebuild` and `charts` needs `ENV=<name>`.

You can also check the output variables with:

```shell
make output ENV=<name>
```

e.g., you should see something like:

```shell
monitoring_ssh_host = "18.19.14.13"
public_key_path = "~/.ssh/kafka_aws.pub"
username = "ec2-user"
worker_ssh_host = "18.16.14.86"
```

Now that the infrastructure is provisioned, you can run Ansible to install and setup OMB workers.

Fist, let's verify that the Ansible Galaxy roles are installed:

```shell
ansible-galaxy install -r ansible-galaxy.yaml
```

Ensure OMB binaries are built and available locally. You can build them with:

```shell
make build
```

Then run the Ansible playbook:

```shell
ansible-playbook -i envs/<name>/hosts.yaml deploy.yaml
```

The ansible playbook has some variables that you can customize, e.g.:

|     Variable     |                                   Description                                    | Default Value |
|------------------|----------------------------------------------------------------------------------|---------------|
| `cli_jvm_mem`    | JVM memory for OMB CLI tool. Sometimes needs to be increased for large datasets. | `1G`          |
| `worker_jvm_mem` | JVM memory for OMB workers. Increase to use most of the VM memory.               | `4G`          |

You can also run the playbook with `make`:

```shell
make deploy ENV=<name>
# and tune JVM memory
make CLI_JVM_MEM=8G WORKER_JVM_MEM=40G deploy ENV=<name>
# and pass ansible extra vars
make ANSIBLE_ARGS="--limit monitoring" deploy ENV=<name>
```

Ansible will install and setup OMB workers, start the monitoring stack, and configure the necessary dashboards in Grafana.

Run the following commands to validate that workers are running:

```shell
make check_workers ENV=<name>
```

and open Grafana at `http://<monitoring_ssh_host>:3000` (default credentials: `admin/admin`)
and Prometheus at `http://<monitoring_ssh_host>:9090`.

```shell
make open_grafana ENV=<name>
make open_prometheus ENV=<name>
```

### Preparing OMB plans

You can create multiple driver configurations in the [`drivers/`](./drivers) directory. Each YAML file in this directory will be processed as a separate driver configuration.

For example:
- `drivers/default.yaml` - Basic Kafka configuration
- `drivers/diskless-low-throughput.yaml` - Diskless configuration for low throughput
- `drivers/diskless-high-throughput.yaml` - Diskless configuration optimized for high throughput

When uploaded, each driver template will be named with the pattern `{svc_name}-{template_name}.yaml` on the worker nodes. For instance, if your service is named `my-kafka` and you have `drivers/diskless-high-throughput.yaml`, it will be uploaded as `/etc/benchmark/drivers/my-kafka-diskless-high-throughput.yaml`. Workloads are uploaded to `/etc/benchmark/workloads/`; files that are no longer in [`./workloads`](./workloads) are deleted from the nodes.

Adapt the OMB driver templates to your needs, e.g., set the topic and client configurations.

The playbook will use your `avn` CLI to gather the necessary connection information for Kafka clusters.

Then, validate and add any missing workload plans to the [`./workloads`](./workloads) directory.

Once all is set locally, run the Ansible playbook to upload the plans to OMB workers:

```shell
ansible-playbook -i envs/<name>/hosts.yaml upload.yaml -e svc_name=<your-service-name>
```

or with `make`:

```shell
make upload_plans ENV=<name>
```

To use a Kafka service created with [`deployment/aiven-terraform`](../aiven-terraform) instead of the `avn` CLI, pass its Terraform environment name:

```shell
make upload_plans ENV=<name> AIVEN_ENV=<aiven-env>
```

The playbook reads the service URI, CA certificate, and client certificate and key from the outputs of that environment. It prefixes the driver files with the environment name, for example `<aiven-env>-default.yaml`, so each environment keeps its own driver set in `/etc/benchmark/drivers` even when the services have the same name. It needs the Terraform state of that environment on your machine. `SVC_NAME` is ignored in this case.
By default the playbook reads the state in `deployment/aiven-terraform` of the same checkout. If the state is in another directory, set `AIVEN_TF_DIR`, for example `export AIVEN_TF_DIR=<path>/deployment/aiven-terraform`.

### Run the workloads

Now that everything is set up, you can start running the workloads by connecting to one of the OMB workers via SSH:

```shell
ssh -i <private_key_path> <username>@<worker_ssh_host>
```

or with `make`:

```shell
make connect ENV=<name>
```

Once connected, you can start OMB workloads with:

```shell
sudo -i
cd /opt/benchmark
# List available drivers
ls /etc/benchmark/drivers/
# Run with a specific driver configuration
./bin/benchmark --drivers /etc/benchmark/drivers/$SVC_NAME-diskless-high-throughput.yaml --workloads /etc/benchmark/workloads/$WORKLOAD_NAME.yaml
# or with the default driver
./bin/benchmark --drivers /etc/benchmark/drivers/$SVC_NAME-default.yaml --workloads /etc/benchmark/workloads/$WORKLOAD_NAME.yaml
# optionally add -x to increase the number of consumer nodes to 2/3 of the available nodes
./bin/benchmark --drivers /etc/benchmark/drivers/$SVC_NAME-diskless-high-throughput.yaml --workloads /etc/benchmark/workloads/$WORKLOAD_NAME.yaml -x
```

### Explore the results

Use Grafana dashboards to explore the metrics collected during the workload runs.

Or run plotting scripts locally by copying the results from the worker nodes to your local machine:

```shell
make download_results ENV=<name>
```

and then run the plotting scripts:

```shell
make plot_results
```

Which passes the JSON to the plotting script at `bin/create_charts.py`.
