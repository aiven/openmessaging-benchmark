# Local Aiven Kafka Benchmark
#
# Usage:
#   make setup SERVICE=my-kafka-service
#   make run SERVICE=my-kafka-service
#   make run SERVICE=my-kafka-service DRIVER=diskless
#   make run SERVICE=my-kafka-service DRIVER=throughput WORKLOAD=local-50k
#   make clean
#
# Prerequisites:
#   - avn CLI installed and authenticated (pip install aiven-client && avn user login)
#   - jq installed
#   - Java 17+ and Maven (for building OMB)

SERVICE ?=
DRIVER ?= default
WORKLOAD ?= local-10k
HEAP_OPTS ?= -Xms1G -Xmx1G

ROOT_DIR := $(shell cd ../.. && pwd)
GENERATED_DIR := $(CURDIR)/generated
DRIVER_CONFIG := $(GENERATED_DIR)/$(SERVICE)-$(DRIVER).yaml
WORKLOAD_FILE := $(ROOT_DIR)/workloads/local/$(WORKLOAD).yaml
BENCHMARK_BIN := $(ROOT_DIR)/bin/benchmark

.PHONY: setup run clean build help list-workloads list-drivers

help:
	@echo "Local Aiven Kafka Benchmark"
	@echo ""
	@echo "Targets:"
	@echo "  setup   - Fetch credentials and render all driver configs"
	@echo "  run     - Run benchmark (calls setup if needed)"
	@echo "  clean   - Remove generated credentials and configs"
	@echo "  build   - Build OMB (Maven)"
	@echo ""
	@echo "Variables:"
	@echo "  SERVICE   = $(SERVICE)  (required)"
	@echo "  DRIVER    = $(DRIVER)"
	@echo "  WORKLOAD  = $(WORKLOAD)"
	@echo "  HEAP_OPTS = $(HEAP_OPTS)"
	@echo ""
	@echo "Available workloads:  make list-workloads"
	@echo "Available drivers:    make list-drivers"

list-workloads:
	@echo "Local workloads (workloads/local/):"
	@ls $(ROOT_DIR)/workloads/local/*.yaml 2>/dev/null | xargs -n1 basename | sed 's/\.yaml$$//'
	@echo ""
	@echo "All workloads can be used with full path:"
	@echo "  make run WORKLOAD=../../workloads/1-topic-1-partition-1kb"

list-drivers:
	@echo "Available driver templates:"
	@ls drivers/*.yaml | xargs -n1 basename | sed 's/\.yaml$$//'

setup:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required. Usage: make setup SERVICE=my-kafka"; exit 1; fi
	SERVICE=$(SERVICE) ./setup.sh

build:
	$(MAKE) -C $(ROOT_DIR) build

rebuild:
	$(MAKE) -C $(ROOT_DIR) clean build

run: build
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required. Usage: make run SERVICE=my-kafka"; exit 1; fi
	@if [ ! -f "$(DRIVER_CONFIG)" ]; then echo "==> Running setup first..."; $(MAKE) setup; fi
	@if [ ! -f "$(DRIVER_CONFIG)" ]; then \
		echo "ERROR: Driver config not found: $(DRIVER_CONFIG)"; \
		echo "Available drivers: make list-drivers"; \
		exit 1; \
	fi
	@if [ ! -f "$(WORKLOAD_FILE)" ]; then \
		echo "ERROR: Workload not found: $(WORKLOAD_FILE)"; \
		echo "Available: make list-workloads"; \
		exit 1; \
	fi
	@echo ""
	@echo "==> Running benchmark"
	@echo "    Driver:   $(DRIVER_CONFIG)"
	@echo "    Workload: $(WORKLOAD_FILE)"
	@echo "    Heap:     $(HEAP_OPTS)"
	@echo ""
	cd $(ROOT_DIR) && HEAP_OPTS="$(HEAP_OPTS)" $(BENCHMARK_BIN) \
		--drivers $(DRIVER_CONFIG) \
		$(WORKLOAD_FILE)

clean:
	rm -rf $(GENERATED_DIR)
	@echo "Cleaned generated configs."
