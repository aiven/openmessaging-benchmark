#!/usr/bin/env bash
# Create or wire observability services (OpenSearch + Thanos) for an Aiven Kafka service.
#
# Creates k-aws-logs and k-aws-metrics if they don't exist, then enables
# logs + metrics + jolokia integrations on the target Kafka service.
#
# Usage:
#   SERVICE=k-aws-1 ./create-observability.sh
#   SERVICE=k-aws-1 ./create-observability.sh teardown   # remove obs services + integrations
#
# Required env:
#   AIVEN_WEB_URL  — Aiven REST endpoint
#   SERVICE        — Kafka service name to instrument
#
# Optional env:
#   CLOUD          — cloud region  (default: aws-eu-west-1)
#   LOGS_SVC       — OpenSearch service name  (default: k-aws-logs)
#   METRICS_SVC    — Thanos service name      (default: k-aws-metrics)
#   OS_PLAN        — OpenSearch plan  (default: startup-4)
#   THANOS_PLAN    — Thanos plan      (default: startup-4)
#   JOLOKIA_USER   — Jolokia basic-auth username  (default: kawsjol1)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

CLOUD="${CLOUD:-aws-eu-west-1}"
OS_PLAN="${OS_PLAN:-startup-4}"
THANOS_PLAN="${THANOS_PLAN:-startup-4}"
JOLOKIA_USER="${JOLOKIA_USER:-kawsjol1}"

# ---- teardown ---------------------------------------------------------------
if [[ "${1:-}" == "teardown" ]]; then
  echo "=== Tearing down observability for $SERVICE ==="

  # Remove integrations first
  for itype in logs metrics jolokia; do
    id=$(avn_q service integration-list "$SERVICE" --json 2>/dev/null \
      | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    if i['integration_type'] == '$itype' and i.get('enabled'):
        print(i['service_integration_id'])
" 2>/dev/null || true)
    if [[ -n "$id" ]]; then
      echo "  Removing $itype integration ($id)..."
      avn_q service integration-delete "$id" || true
    fi
  done

  # Remove jolokia endpoint if it exists
  ep_id=$(avn_q service integration-endpoint-list --project test --json 2>/dev/null \
    | python3 -c "
import sys, json
for e in json.load(sys.stdin):
    if e['endpoint_type'] == 'jolokia':
        print(e['endpoint_id'])
" 2>/dev/null || true)
  if [[ -n "$ep_id" ]]; then
    echo "  Removing jolokia endpoint ($ep_id)..."
    avn_q service integration-endpoint-delete "$ep_id" --project test || true
  fi

  echo "  Deleting $LOGS_SVC..."
  avn_q service terminate "$LOGS_SVC" --force 2>/dev/null || echo "  ($LOGS_SVC not found)"
  echo "  Deleting $METRICS_SVC..."
  avn_q service terminate "$METRICS_SVC" --force 2>/dev/null || echo "  ($METRICS_SVC not found)"
  echo "Done."
  exit 0
fi

# ---- create / ensure services -----------------------------------------------
echo "=== Observability for $SERVICE ==="
echo "  Cloud:   $CLOUD"
echo "  Logs:    $LOGS_SVC ($OS_PLAN)"
echo "  Metrics: $METRICS_SVC ($THANOS_PLAN)"
echo ""

_svc_exists() { avn_q service get "$1" --json &>/dev/null; }

if ! _svc_exists "$LOGS_SVC"; then
  echo "Creating $LOGS_SVC (OpenSearch $OS_PLAN)..."
  avn_q service create "$LOGS_SVC" \
    --service-type opensearch \
    --plan "$OS_PLAN" \
    --cloud "$CLOUD"
else
  echo "$LOGS_SVC already exists — skipping creation."
fi

if ! _svc_exists "$METRICS_SVC"; then
  echo "Creating $METRICS_SVC (Thanos $THANOS_PLAN)..."
  avn_q service create "$METRICS_SVC" \
    --service-type thanos \
    --plan "$THANOS_PLAN" \
    --cloud "$CLOUD"
else
  echo "$METRICS_SVC already exists — skipping creation."
fi

# ---- jolokia endpoint -------------------------------------------------------
ep_id=$(avn_q service integration-endpoint-list --project test --json 2>/dev/null \
  | python3 -c "
import sys, json
for e in json.load(sys.stdin):
    if e['endpoint_type'] == 'jolokia':
        print(e['endpoint_id'])
" 2>/dev/null || true)

if [[ -z "$ep_id" ]]; then
  JOLOKIA_PASSWORD="${JOLOKIA_PASSWORD:-$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 20)}"
  echo "Creating jolokia endpoint (user: $JOLOKIA_USER)..."
  ep_id=$(avn_q service integration-endpoint-create \
    --project test \
    --endpoint-name "${SERVICE}-jolokia" \
    --endpoint-type jolokia \
    --user-config "{\"basic_auth_username\":\"$JOLOKIA_USER\",\"basic_auth_password\":\"$JOLOKIA_PASSWORD\"}" \
    --json | python3 -c "import sys,json; print(json.load(sys.stdin)['endpoint_id'])")
  echo "  endpoint_id: $ep_id"
  echo "  JOLOKIA_USER:     $JOLOKIA_USER"
  echo "  JOLOKIA_PASSWORD: $JOLOKIA_PASSWORD  <-- save this"
else
  echo "Jolokia endpoint already exists ($ep_id) — skipping."
fi

# ---- wait for services -------------------------------------------------------
echo ""
echo "Waiting for $LOGS_SVC..."
avn_q service wait "$LOGS_SVC"
echo "$LOGS_SVC RUNNING"

echo "Waiting for $METRICS_SVC..."
avn_q service wait "$METRICS_SVC"
echo "$METRICS_SVC RUNNING"

# ---- integrations ------------------------------------------------------------
echo ""

_integration_exists() {
  avn_q service integration-list "$SERVICE" --json 2>/dev/null \
    | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    if i['integration_type'] == '$1' and i.get('enabled'):
        raise SystemExit(0)
raise SystemExit(1)" 2>/dev/null
}

if ! _integration_exists logs; then
  echo "Wiring logs: $SERVICE → $LOGS_SVC..."
  avn_q service integration-create \
    --project test \
    --source-service "$SERVICE" \
    --dest-service "$LOGS_SVC" \
    --integration-type logs
fi

if ! _integration_exists metrics; then
  echo "Wiring metrics: $SERVICE → $METRICS_SVC..."
  avn_q service integration-create \
    --project test \
    --source-service "$SERVICE" \
    --dest-service "$METRICS_SVC" \
    --integration-type metrics
fi

if ! _integration_exists jolokia; then
  echo "Wiring jolokia: $SERVICE → endpoint $ep_id..."
  avn_q service integration-create \
    --project test \
    --source-service "$SERVICE" \
    --dest-endpoint-id "$ep_id" \
    --integration-type jolokia
fi

# ---- summary -----------------------------------------------------------------
echo ""
echo "=== Done ==="
OS_URI=$(avn_q service get "$LOGS_SVC" --json | python3 -c "import sys,json; print(json.load(sys.stdin)['service_uri'])")
THANOS_HOST=$(avn_q service get "$METRICS_SVC" --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = next(c for c in d['components'] if c['component'] == 'query_frontend')
creds = d['service_uri'].split('@')[0].split('//')[1]
print(f'https://{creds}@{c[\"host\"]}:{c[\"port\"]}')")
JOLOKIA_HOST=$(avn_q service get "$SERVICE" --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = next((c for c in d['components'] if c['component'] == 'jolokia'), None)
print(f'{c[\"host\"]}:{c[\"port\"]}' if c else '(not yet active)')")

echo "  OpenSearch (logs):   $OS_URI"
echo "  Thanos query:        $THANOS_HOST/api/v1/query"
echo "  Jolokia:             https://<user>:<pass>@$JOLOKIA_HOST/jolokia/read/"
echo ""
echo "Export for observe scripts:"
echo "  export AIVEN_WEB_URL=$AIVEN_WEB_URL"
echo "  export SERVICE=$SERVICE"
echo "  export JOLOKIA_USER=$JOLOKIA_USER"
echo "  export JOLOKIA_PASSWORD=<saved above>"
