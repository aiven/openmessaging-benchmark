#!/usr/bin/env bash
# Surface recent ERROR/WARN/Exception lines for a service.
#
# Usage:
#   SERVICE=k-aws-1 ./check-errors.sh
#   SERVICE=k-aws-1 ./check-errors.sh 50          # show 50 hits
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

SIZE="${1:-25}"

echo "=== Recent ERROR/Exception ($SERVICE) ==="
svc_search "ERROR OR Exception OR FATAL" "$SIZE" | print_hits 220
