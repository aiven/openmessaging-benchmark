all: provision deploy

# Each environment has envs/<ENV>.tfvars (see envs/example.tfvars), its own Terraform
# workspace, and its own Ansible inventory at envs/<ENV>/hosts.yaml.
ENV ?=
TF := terraform -chdir=terraform/aws
export TF_WORKSPACE = $(ENV)
INVENTORY = envs/$(ENV)/hosts.yaml
TF_VARS = -var-file=$(CURDIR)/envs/$(ENV).tfvars -var inventory_path=$(CURDIR)/$(INVENTORY)
ANSIBLE_INVENTORY=$(shell which terraform-inventory)

NO_ENV_GOALS := ssh_key build rebuild charts .venv
ifneq ($(filter-out $(NO_ENV_GOALS),$(or $(MAKECMDGOALS),all)),)
ifeq ($(ENV),)
$(error ENV is required, e.g. make provision ENV=dev. Copy envs/example.tfvars to envs/<ENV>.tfvars)
endif
endif

SSH_ALG=ed25519

ssh_key:
	ssh-keygen -f ~/.ssh/omb-deploy -t $(SSH_ALG)
	ssh-add ~/.ssh/omb-deploy

.PHONY: check-tfvars
check-tfvars:
	@test -f envs/$(ENV).tfvars || { echo "ERROR: envs/$(ENV).tfvars not found. Copy envs/example.tfvars."; exit 1; }

provision: check-tfvars
	mkdir -p envs/$(ENV)
	$(TF) init
	$(TF) apply -auto-approve $(TF_VARS)

refresh: check-tfvars
	$(TF) refresh $(TF_VARS)

destroy: check-tfvars
	$(TF) destroy $(TF_VARS)

build:
	$(MAKE) -C ../.. build

rebuild:
	$(MAKE) -C ../.. clean
	$(MAKE) -C ../.. build

CLI_JVM_MEM=2G
WORKER_JVM_MEM=4G
ANSIBLE_ARGS=

deploy: build
	ansible-galaxy install -r ansible-galaxy.yaml
	ansible-playbook -i $(INVENTORY) -e "cli_jvm_mem=$(CLI_JVM_MEM)" -e "worker_jvm_mem=$(WORKER_JVM_MEM)" $(ANSIBLE_ARGS) deploy.yaml
WORKER_JVM_MEM=4G

check_workers:
	ansible -i $(INVENTORY) \
    	-m shell -a "systemctl status benchmark-worker" worker

restart_worker:
	ansible -i $(INVENTORY) \
    	-m shell -a "systemctl restart benchmark-worker" worker

stop_firewall:
	ansible -i $(INVENTORY) \
    	-m shell -a "sudo systemctl stop firewalld" worker

IP ?=

connect:
	ssh -i $(shell $(TF) output -raw public_key_path) $(shell $(TF) output -raw username)@$(shell if [ -n "$$IP" ]; then echo "$$IP"; else $(TF) output -raw worker_ssh_host; fi)

SVC_NAME=kafka

upload_plans:
	ansible-playbook -i $(INVENTORY) -e "svc_name=$(SVC_NAME)" $(ANSIBLE_ARGS) upload.yaml

CLIENT_VERSION=4.0.0

upload_kafka_client: 
	# remove existing libraries
	ansible -i $(INVENTORY) \
    	-m shell -a "rm -rf /opt/benchmark/lib/*kafka-clients-*" client
  # add custom library
	ansible -i $(INVENTORY) \
    	-m copy -a "src=package/kafka-clients-$(CLIENT_VERSION).jar dest=/opt/benchmark/lib/" client

TIMESTAMP := $(shell date +%Y%m%d_%H%M%S)

download_results:
	scp -i $(shell $(TF) output -raw public_key_path) $(shell $(TF) output -raw username)@$(shell $(TF) output -raw worker_ssh_host):'/opt/benchmark/*.json' .

download_logs:
	ansible -i $(INVENTORY) \
    -m fetch -a "src=/opt/benchmark/benchmark-worker.log dest=./benchmark-worker_{{ inventory_hostname }}.log flat=yes" worker

clean_logs:
	ansible -i $(INVENTORY) \
    -m shell -a "rm -rf /opt/benchmark/benchmark-worker.log" worker

output:
	$(TF) output

.venv:
	python3 -m venv .venv

RESULT_DIR=.

charts: .venv
	source .venv/bin/activate && \
	.venv/bin/python3 -m pip install -r requirements.txt
	for f in $(shell ls $(RESULT_DIR)/*.json); do \
		.venv/bin/python3 ../../bin/create_charts.py $$f; \
	done

connect_monitoring:
	ssh -i $(shell $(TF) output -raw public_key_path) $(shell $(TF) output -raw username)@$(shell $(TF) output -raw monitoring_ssh_host)

open_prometheus:
	open http://$(shell $(TF) output -raw monitoring_ssh_host):9090

open_grafana:
	open http://$(shell $(TF) output -raw monitoring_ssh_host):3000
