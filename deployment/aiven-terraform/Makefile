# Usage: make plan ENV=dev        (reads envs/dev.tfvars, keeps one state per ENV)
#        make apply ENV=dev
#        make destroy ENV=dev
ENV ?=
TF_ARGS = -var-file=envs/$(ENV).tfvars

.PHONY: init plan apply destroy output check-env

check-env:
	@if [ -z "$(ENV)" ]; then echo "ERROR: ENV is required, e.g. make plan ENV=dev"; exit 1; fi
	@if [ ! -f envs/$(ENV).tfvars ]; then echo "ERROR: envs/$(ENV).tfvars not found (copy envs/example.tfvars)"; exit 1; fi

init:
	terraform init

plan apply destroy: check-env init
	terraform workspace select -or-create $(ENV)
	terraform $@ $(TF_ARGS)

output: check-env init
	terraform workspace select -or-create $(ENV)
	terraform output
