#!/usr/bin/env bash
# One-shot consolidation health snapshot: throughput, fetcher activity, lag, errors.
# Designed to be run repeatedly while a benchmark is in flight.
#
# Usage:
#   SERVICE=k-aws-1 ./monitor-consolidation.sh
#   watch -n 30 'SERVICE=k-aws-1 ./monitor-consolidation.sh'
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

echo "############ $SERVICE @ $(date -u +%H:%M:%SZ) ############"

echo "--- BytesIn MiB/s (1m) ---"
q "sum by (service,host)(rate(kafka_server_BrokerTopicMetrics_BytesInPerSec_Count{service=\"$SERVICE\"}[1m]))" 1048576

echo "--- BytesOut MiB/s (1m) ---"
q "sum by (service,host)(rate(kafka_server_BrokerTopicMetrics_BytesOutPerSec_Count{service=\"$SERVICE\"}[1m]))" 1048576

echo "--- Consolidation FetchRate (cumulative count; must climb) ---"
q "sum by (service)(io_aiven_inkless_consolidation_ConsolidationFetchMetrics_FetchRate_Count{service=\"$SERVICE\"})"

echo "--- Consolidation FindBatches count ---"
q "sum by (service)(io_aiven_inkless_consolidation_ConsolidationFetchMetrics_FindBatchesTime_Count{service=\"$SERVICE\"})"

echo "--- Fetch/FindBatches ERROR counts (want ~0) ---"
q "sum by (service)(io_aiven_inkless_consolidation_ConsolidationFetchMetrics_FetchErrorRate_Count{service=\"$SERVICE\"})"
q "sum by (service)(io_aiven_inkless_consolidation_ConsolidationFetchMetrics_FindBatchesErrorRate_Count{service=\"$SERVICE\"})"

echo "--- ConsolidationLocalLag per broker (diskless HW - local LEO) ---"
q "io_aiven_inkless_consolidation_ConsolidationMetrics_ConsolidationLocalLag_Value{service=\"$SERVICE\"}"

echo "--- ConsolidationTotalLag per broker (diskless HW - remote LEO) ---"
q "io_aiven_inkless_consolidation_ConsolidationMetrics_ConsolidationTotalLag_Value{service=\"$SERVICE\"}"

echo "--- FetchTotalTime mean (ms) ---"
q "io_aiven_inkless_consolidation_ConsolidationFetchMetrics_FetchTotalTime_Mean{service=\"$SERVICE\"}"

echo "--- DeletableMessages per broker ---"
q "io_aiven_inkless_consolidation_ConsolidationMetrics_ConsolidationDeletableMessages_Value{service=\"$SERVICE\"}"

echo ""
echo "--- Recent ERROR/Exception ---"
"$DIR/check-errors.sh"
