#!/usr/bin/env bash
# Verify consolidation is active: fetcher manager activity, InklessConfig dump, recent errors.
#
# Usage:
#   SERVICE=k-aws-1 ./check-consolidation.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

echo "=== ConsolidationFetcherManager activity ($SERVICE) ==="
svc_search "ConsolidationFetcherManager" 8 | print_hits 140

echo ""
echo "=== InklessConfig dump (latest) ==="
os_search "{
  \"size\": 1,
  \"query\":{\"bool\":{\"must\":[
    $(svc_filter),
    {\"match_phrase\":{\"MESSAGE\":\"InklessConfig values\"}}
  ]}},
  \"sort\":[{\"timestamp\":\"desc\"}],
  \"_source\":[\"timestamp\",\"HOSTNAME\",\"MESSAGE\"]
}" | print_hits 400

echo ""
echo "=== Consolidation fetcher ERRORs/WARNs ==="
svc_search "ConsolidationFetcher* AND (ERROR OR WARN OR Exception)" 10 | print_hits 200
