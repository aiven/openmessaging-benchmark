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

Supported VMs:

## How to use

Follow the Cloud provider module guide to create a `main.tf` file:

- [AWS EC2](./terraform/aws/README.md)

Setup the credentials depending on the Cloud provider, and then run:

```shell
terraform init
terraform apply
```

or

```shell
make provision
# or use a different directory
make TF_DIR=aws1 provision
```

If it finishes successfully, an Ansible inventory file will be generated at `hosts.yaml`.

You can also check the output variables with:

```shell
terraform output
```

or

```shell
make output
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
ansible-playbook -i hosts.yaml deploy.yaml
```

The ansible playbook has some variables that you can customize, e.g.:

|     Variable     |                                   Description                                    | Default Value |
|------------------|----------------------------------------------------------------------------------|---------------|
| `cli_jvm_mem`    | JVM memory for OMB CLI tool. Sometimes needs to be increased for large datasets. | `1G`          |
| `worker_jvm_mem` | JVM memory for OMB workers. Increase to use most of the VM memory.               | `4G`          |

You can also run the playbook with `make`:

```shell
make deploy
# or target a different directory
make TF_DIR=aws1 deploy
# and tune JVM memory
make CLI_JVM_MEM=8G WORKER_JVM_MEM=40G deploy
# and pass ansible extra vars
make ANSIBLE_ARGS="--limit monitoring" deploy
```

Ansible will install and setup OMB workers, start the monitoring stack, and configure the necessary dashboards in Grafana.

Run the following commands to validate that workers are running:

```shell
make check_workers
```

and open Grafana at `http://<monitoring_ssh_host>:3000` (default credentials: `admin/admin`)
and Prometheus at `http://<monitoring_ssh_host>:9090`.

```shell
make open_grafana
make open_prometheus
```

### Preparing OMB plans

Adapt the OMB driver template at [`drivers/kafka.yaml`](./drivers/kafka.yaml) to your needs, e.g., set the topic, and client configurations.

The playbook will use your `avn` CLI to gather the necessary connection information for Kafka clusters.

Then, validate and add any missing workload plans to the [`./workloads`](./workloads) directory.

Once all is set locally, run the Ansible playbook to upload the plans to OMB workers:

```shell
ansible-playbook -i hosts.yaml upload_plans.yaml
```

or with `make`:

```shell
make upload_plans
# or target a different directory
make TF_DIR=aws1 upload_plans
```

### Run the workloads

Now that everything is set up, you can start running the workloads by connecting to one of the OMB workers via SSH:

```shell
ssh -i <private_key_path> <username>@<worker_ssh_host>
```

or with `make`:

```shell
make connect
# or target a different directory
make TF_DIR=aws1 connect
```

Once connected, you can start OMB workloads with:

```shell
sudo -i
cd /opt/benchmark
./bin/benchmark --drivers plans/drivers/$SVC_NAME.yaml --workloads workloads/$WORKLOAD_NAME.yaml
# optionally add -x to increase the number of consumer nodes to 2/3 of the available nodes
./bin/benchmark --drivers plans/drivers/$SVC_NAME.yaml --workloads workloads/$WORKLOAD_NAME.yaml -x
```

### Explore the results

Use Grafana dashboards to explore the metrics collected during the workload runs.

Or run plotting scripts locally by copying the results from the worker nodes to your local machine:

```shell
make download_results
```

and then run the plotting scripts:

```shell
make plot_results
```

Which passes the JSON to the plotting script at `bin/create_charts.py`.
