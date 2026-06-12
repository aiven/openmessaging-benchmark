# Local Aiven Kafka Benchmark
#
# Usage:
#   make setup SERVICE=my-kafka-service
#   make run SERVICE=my-kafka-service
#   make run SERVICE=my-kafka-service DRIVER=diskless
#   make run SERVICE=my-kafka-service DRIVER=throughput WORKLOAD=local-50k
#   make observe SERVICE=my-kafka-service
#   make observe-teardown SERVICE=my-kafka-service
#   make monitor SERVICE=my-kafka-service
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

LOGS_SVC       ?= k-aws-logs
METRICS_SVC    ?= k-aws-metrics
JOLOKIA_USER   ?= kawsjol1
JOLOKIA_PASSWORD ?=

ROOT_DIR := $(shell cd ../.. && pwd)
GENERATED_DIR := $(CURDIR)/generated
DRIVER_CONFIG := $(GENERATED_DIR)/$(SERVICE)-$(DRIVER).yaml
WORKLOAD_FILE := $(ROOT_DIR)/workloads/local/$(WORKLOAD).yaml
BENCHMARK_BIN := $(ROOT_DIR)/bin/benchmark

OBSERVE_ENV = AIVEN_WEB_URL=$(AIVEN_WEB_URL) SERVICE=$(SERVICE) \
              LOGS_SVC=$(LOGS_SVC) METRICS_SVC=$(METRICS_SVC) \
              JOLOKIA_USER=$(JOLOKIA_USER) JOLOKIA_PASSWORD=$(JOLOKIA_PASSWORD)

.PHONY: setup run clean build help list-workloads list-drivers \
        observe observe-teardown monitor check-errors check-throughput \
        check-consolidation check-version

help:
	@echo "Local Aiven Kafka Benchmark"
	@echo ""
	@echo "Benchmark targets:"
	@echo "  setup              - Fetch credentials and render all driver configs"
	@echo "  run                - Run benchmark (calls setup if needed)"
	@echo "  clean              - Remove generated credentials and configs"
	@echo "  build              - Build OMB (Maven)"
	@echo ""
	@echo "Observability targets:"
	@echo "  observe            - Create OpenSearch + Thanos + Jolokia for SERVICE"
	@echo "  observe-teardown   - Remove observability services and integrations"
	@echo "  monitor            - Consolidation health snapshot (run repeatedly)"
	@echo "  check-errors       - Recent ERRORs/Exceptions in logs"
	@echo "  check-throughput   - BytesIn/BytesOut per broker via Thanos"
	@echo "  check-consolidation- Fetcher activity + InklessConfig dump"
	@echo "  check-version      - Kafka commitId per node"
	@echo ""
	@echo "Variables:"
	@echo "  SERVICE          = $(SERVICE)  (required)"
	@echo "  DRIVER           = $(DRIVER)"
	@echo "  WORKLOAD         = $(WORKLOAD)"
	@echo "  LOGS_SVC         = $(LOGS_SVC)"
	@echo "  METRICS_SVC      = $(METRICS_SVC)"
	@echo "  JOLOKIA_USER     = $(JOLOKIA_USER)"
	@echo "  JOLOKIA_PASSWORD = (set via env)"
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

# --- Observability -----------------------------------------------------------

observe:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	@if [ -z "$(AIVEN_WEB_URL)" ]; then echo "ERROR: AIVEN_WEB_URL is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/create-observability.sh

observe-teardown:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	@if [ -z "$(AIVEN_WEB_URL)" ]; then echo "ERROR: AIVEN_WEB_URL is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/create-observability.sh teardown

monitor:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/monitor-consolidation.sh

check-errors:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/check-errors.sh

check-throughput:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/check-throughput.sh

check-consolidation:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/check-consolidation.sh

check-version:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(OBSERVE_ENV) ./observe/check-version.sh
