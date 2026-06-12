#!/usr/bin/env bash
# Broker-side throughput (BytesIn/BytesOut) per service via Thanos.
#
# Usage:
#   SERVICE=k-aws-1 ./check-throughput.sh
#   SERVICE=k-aws-1 ./check-throughput.sh 5m     # use 5m rate window instead of 1m
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

WINDOW="${1:-1m}"

echo "=== BytesIn (broker, MiB/s, ${WINDOW} rate) ==="
q "sum by (service,host)(rate(kafka_server_BrokerTopicMetrics_BytesInPerSec_Count{service=\"$SERVICE\"}[$WINDOW]))" 1048576

echo "=== BytesOut (broker, MiB/s, ${WINDOW} rate) ==="
q "sum by (service,host)(rate(kafka_server_BrokerTopicMetrics_BytesOutPerSec_Count{service=\"$SERVICE\"}[$WINDOW]))" 1048576

echo "=== MessagesIn (broker, msg/s, ${WINDOW} rate) ==="
q "sum by (service,host)(rate(kafka_server_BrokerTopicMetrics_MessagesInPerSec_Count{service=\"$SERVICE\"}[$WINDOW]))"
