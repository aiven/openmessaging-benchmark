all: provision deploy

TF_DIR=.
ANSIBLE_INVENTORY=$(shell which terraform-inventory)

SSH_ALG=ed25519

ssh_key:
	ssh-keygen -f ~/.ssh/omb-deploy -t $(SSH_ALG)
	ssh-add ~/.ssh/omb-deploy

provision:
	cd $(TF_DIR) && \
	terraform init && \
	terraform apply -auto-approve

refresh:
	cd $(TF_DIR) && \
	terraform refresh

destroy:
	cd $(TF_DIR) && terraform destroy

../../package/target/openmessaging-benchmark-0.0.1-SNAPSHOT-bin.tar.gz:
	cd ../.. && ./mvnw clean package -Dlicense.skip=true -DskipTests

build: ../../package/target/openmessaging-benchmark-0.0.1-SNAPSHOT-bin.tar.gz

CLI_JVM_MEM=2G
WORKER_JVM_MEM=4G
ANSIBLE_ARGS=

deploy: build
	ansible-galaxy install -r ansible-galaxy.yaml
	ansible-playbook -i $(TF_DIR)/hosts.yaml -e "cli_jvm_mem=$(CLI_JVM_MEM)" -e "worker_jvm_mem=$(WORKER_JVM_MEM)" $(ANSIBLE_ARGS) deploy.yaml
WORKER_JVM_MEM=4G

check_workers:
	ansible -i $(TF_DIR)/hosts.yaml \
    	-m shell -a "systemctl status benchmark-worker" worker

restart_worker:
	ansible -i $(TF_DIR)/hosts.yaml \
    	-m shell -a "systemctl restart benchmark-worker" worker

stop_firewall:
	ansible -i $(TF_DIR)/hosts.yaml \
    	-m shell -a "sudo systemctl stop firewalld" worker

IP ?=

connect:
	ssh -i $(shell cd $(TF_DIR); terraform output -raw public_key_path) $(shell cd $(TF_DIR); terraform output -raw username)@$(shell if [ -n "$$IP" ]; then echo "$$IP"; else cd $(TF_DIR); terraform output -raw worker_ssh_host; fi)

SVC_NAME=kafka

upload_plans:
	ansible-playbook -i $(TF_DIR)/hosts.yaml -e "svc_name=$(SVC_NAME)" $(ANSIBLE_ARGS) upload.yaml

CLIENT_VERSION=4.0.0

upload_kafka_client: 
	# remove existing libraries
	ansible -i $(TF_DIR)/hosts.yaml \
    	-m shell -a "rm -rf /opt/benchmark/lib/*kafka-clients-*" client
  # add custom library
	ansible -i $(TF_DIR)/hosts.yaml \
    	-m copy -a "src=package/kafka-clients-$(CLIENT_VERSION).jar dest=/opt/benchmark/lib/" client

TIMESTAMP := $(shell date +%Y%m%d_%H%M%S)

download_results:
	scp -i $(shell cd $(TF_DIR); terraform output -raw public_key_path) $(shell cd $(TF_DIR); terraform output -raw username)@$(shell cd $(TF_DIR); terraform output -raw worker_ssh_host):'/opt/benchmark/*.json' .

download_logs:
	ansible -i $(TF_DIR)/hosts.yaml \
    -m fetch -a "src=/opt/benchmark/benchmark-worker.log dest=./benchmark-worker_{{ inventory_hostname }}.log flat=yes" worker

clean_logs:
	ansible -i $(TF_DIR)/hosts.yaml \
    -m shell -a "rm -rf /opt/benchmark/benchmark-worker.log" worker

output:
	cd $(TF_DIR) && \
	terraform output

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
	ssh -i $(shell cd $(TF_DIR); terraform output -raw public_key_path) $(shell cd $(TF_DIR); terraform output -raw username)@$(shell cd $(TF_DIR); terraform output -raw monitoring_ssh_host)

open_prometheus:
	open http://$(shell cd $(TF_DIR); terraform output -raw monitoring_ssh_host):9090

open_grafana:
	open http://$(shell cd $(TF_DIR); terraform output -raw monitoring_ssh_host):3000
