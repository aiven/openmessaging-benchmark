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

After a code change, `make patch_omb ENV=<name>` rebuilds the package and replaces the libraries and driver jars on the workers. It restarts the worker service and keeps the `bin` scripts, so the JVM memory settings stay. It does not change the manager node.

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

### Send the metrics to the Aiven Thanos

The Prometheus on the manager node can also write the OMB, JMX and node metrics to the Thanos service of an [`aiven-terraform`](../aiven-terraform) environment. The Aiven Grafana of that environment then shows them with the broker metrics. The Prometheus on the manager node keeps its own copy.

Run `make apply` in `deployment/aiven-terraform` first. The Thanos outputs are new, so apply the environment again if you created it before.

Pass the name of the Terraform environment to `deploy`, and then upload the dashboards:

```shell
make deploy ENV=<name> AIVEN_ENV=<aiven-env>
make upload_dashboards ENV=<name> AIVEN_ENV=<aiven-env>
```

`deploy` reads the remote write URI and the credentials from the outputs of `<aiven-env>`, and turns on `remote_write`. `upload_dashboards` loads the dashboards of [`monitoring/grafana/dashboards`](./monitoring/grafana/dashboards) into the folder `omb` of the Aiven Grafana. It replaces dashboards with the same UID. The dashboards have a variable `Datasource`. Select the Thanos data source in it if Grafana does not pick it.

The Aiven services and the workers must run in the same cloud and region. If they do not, `deploy` prints a warning and continues. The region of the workers comes from the Terraform output `region`. Run `make refresh ENV=<name>` to add it to an existing environment. Without it, the check does not run.

WARNING: The Thanos password is in `/opt/kafka-monitoring/prometheus.yaml` on the manager node. All users of that node can read the file.

### Preparing OMB plans

You can create multiple driver configurations in the [`drivers/`](./drivers) directory. Each YAML file in this directory will be processed as a separate driver configuration.

For example:
- `drivers/default.yaml` - Basic Kafka configuration
- `drivers/diskless-low-throughput.yaml` - Diskless configuration for low throughput
- `drivers/diskless-high-throughput.yaml` - Diskless configuration optimized for high throughput

When uploaded, each driver template will be named with the pattern `{svc_name}-{template_name}.yaml` on the worker nodes and on the manager node (the `monitoring` host). For instance, if your service is named `my-kafka` and you have `drivers/diskless-high-throughput.yaml`, it will be uploaded as `/etc/benchmark/drivers/my-kafka-diskless-high-throughput.yaml`. Workloads are uploaded to `/etc/benchmark/workloads/`; files that are no longer in [`./workloads`](./workloads) are deleted from the nodes.

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

> WARNING: The upload copies the Kafka client key to the manager node, in addition to the worker nodes. The security group allows SSH to the manager node from any address. Use short-lived credentials, and destroy the environment when you finish.

### Delete OMB topics

A workload that uses `topicNames` reuses its topics, and no run deletes topics. To remove the topics of earlier runs, first upload the drivers with `make upload_plans`. Then run:

```shell
make delete_topics ENV=<name> AIVEN_ENV=<aiven-env> TOPICS_REGEX='^<prefix>'
```

Use `SVC_NAME=<service>` instead of `AIVEN_ENV=<aiven-env>` when you uploaded the drivers with the `avn` CLI.
The command runs `kafka-topics.sh` on the first worker, with the client settings from the uploaded `<prefix>-default.yaml`. The prefix is the value of `AIVEN_ENV` or `SVC_NAME`, the same value that `upload_plans` used.
It lists the topics that match `TOPICS_REGEX` and does not delete them.
The default regex is `^test-topic-`, the prefix of the generated topic names. Internal topics never match.
The command fails when `TOPICS_REGEX` is empty.
The playbook deletes the topics in batches of 100, so a large set takes one `kafka-topics.sh` run for each batch.

> WARNING: This command deletes every topic that matches the regex when you add `CONFIRM=true`. Do not run it on a production service.

To delete the listed topics, run the same command with `CONFIRM=true`.

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

### Run the lag recovery orchestrator

`make deploy` installs the [lag recovery orchestrator](../../tools/lag-recovery-benchmark/README.md) on the manager node. It also installs Java, Python 3 and the Kafka command-line tools there.
The orchestrator is in `/opt/benchmark/orchestrator.py`, and the URLs of the workers are in `/opt/benchmark/workers.yaml`.
The orchestrator reaches the workers over the private network.
Upload the drivers with `make upload_plans` first. The upload puts the driver files on the manager node too.

Connect to the manager node, and run the orchestrator as root. The key files in `/etc/benchmark/drivers` are readable by root only.

```shell
make connect_monitoring ENV=<name>
sudo -i
cd /opt/benchmark
nohup python3 orchestrator.py --workers-file workers.yaml --worker-split 2,2,2 \
  --driver-config /etc/benchmark/drivers/<prefix>-default.yaml \
  --kafka-bin /opt/kafka/bin --level 2 --output /opt/benchmark/results.json \
  > orchestrator.log 2>&1 < /dev/null &
tail -f orchestrator.log
```

The command runs in the background with `nohup`, so the run continues if the SSH connection drops. Press Ctrl+C to stop `tail`. The run is not affected. After a reconnect, run `tail -f /opt/benchmark/orchestrator.log` to follow the run. The exit code is not visible in this mode. Read `status` in `results.json` when the run ends.

`<prefix>` is the value of `AIVEN_ENV` or `SVC_NAME` that `make upload_plans` used. Run `ls /etc/benchmark/drivers` to see the prefixes on the node.

The split `2,2,2` gives 2 workers to the producers, 2 to group A and 2 to group B. The orchestrator needs 1 worker for each role of the level. The results stay on the manager node.

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
