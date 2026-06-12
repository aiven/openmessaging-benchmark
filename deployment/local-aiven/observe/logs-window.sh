#!/usr/bin/env bash
# Dump logs for a service within a time window, optionally filtered by a query string.
# Timestamps matched against the OpenSearch `timestamp` field (UTC ISO-8601).
#
# Usage:
#   SERVICE=k-aws-1 ./logs-window.sh <from-iso> <to-iso> [query_string] [size]
#
# Examples:
#   SERVICE=k-aws-1 ./logs-window.sh 2026-06-12T10:00:00 2026-06-12T10:10:00
#   SERVICE=k-aws-1 ./logs-window.sh 2026-06-12T10:00:00 2026-06-12T10:10:00 'ERROR OR Exception' 200
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

FROM="${1:?from-iso required (e.g. 2026-06-12T10:00:00)}"
TO="${2:?to-iso required}"
Q="${3:-}"
SIZE="${4:-100}"

MUST="$(svc_filter),
      {\"range\":{\"timestamp\":{\"gte\":\"$FROM\",\"lte\":\"$TO\"}}}"
if [[ -n "$Q" ]]; then
  MUST="$MUST,
      {\"query_string\":{\"default_field\":\"MESSAGE\",\"query\":\"$Q\"}}"
fi

echo "=== Logs: $SERVICE  $FROM → $TO  ${Q:+(filter: $Q)} ==="
os_search "{
  \"size\": $SIZE,
  \"query\":{\"bool\":{\"must\":[ $MUST ]}},
  \"sort\":[{\"timestamp\":\"asc\"}],
  \"_source\":[\"timestamp\",\"HOSTNAME\",\"MESSAGE\"]
}" | print_hits 240
