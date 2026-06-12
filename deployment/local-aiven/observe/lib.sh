#!/usr/bin/env bash
# Shared helpers for Aiven observability scripts.
# Source this file; it resolves connection info via `avn` and caches URIs.
#
# Required env:
#   AIVEN_WEB_URL  — Aiven REST endpoint
#   SERVICE        — Kafka service to observe (e.g. k-aws-1)
#
# Optional env (defaults shown):
#   LOGS_SVC       — OpenSearch service name     (default: k-aws-logs)
#   METRICS_SVC    — Thanos service name         (default: k-aws-metrics)
#   LOG_DATE       — Date for log index          (default: today UTC, YYYY-MM-DD)
set -euo pipefail

AIVEN_WEB_URL="${AIVEN_WEB_URL:?AIVEN_WEB_URL must be set}"
SERVICE="${SERVICE:?SERVICE must be set (Kafka service name, e.g. k-aws-1)}"
LOGS_SVC="${LOGS_SVC:-k-aws-logs}"
METRICS_SVC="${METRICS_SVC:-k-aws-metrics}"

avn_q() { AIVEN_WEB_URL="$AIVEN_WEB_URL" avn "$@"; }

# Resolve OpenSearch base URI (with credentials).
os_uri() {
  if [[ -z "${_OS_URI:-}" ]]; then
    _OS_URI=$(avn_q service get "$LOGS_SVC" --json \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['service_uri'])")
  fi
  printf '%s' "$_OS_URI"
}

# Resolve Thanos query_frontend URI (with credentials).
thanos_uri() {
  if [[ -z "${_THANOS_URI:-}" ]]; then
    _THANOS_URI=$(avn_q service get "$METRICS_SVC" --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = next(c for c in d['components'] if c['component'] == 'query_frontend')
creds = d['service_uri'].split('@')[0].split('//')[1]  # user:pass
print(f'https://{creds}@{c[\"host\"]}:{c[\"port\"]}')")
  fi
  printf '%s' "$_THANOS_URI"
}

# Resolve Jolokia base URL for a specific broker node (1-indexed).
# Usage: jolokia_url <node-number>    e.g. jolokia_url 1
jolokia_url() {
  local node="${1:?node number required}"
  if [[ -z "${_JOLOKIA_HOST:-}" ]]; then
    _JOLOKIA_HOST=$(avn_q service get "$SERVICE" --json | python3 -c "
import sys, json
d = json.load(sys.stdin)
c = next((c for c in d['components'] if c['component'] == 'jolokia'), None)
if not c: raise SystemExit('no jolokia component — is the jolokia integration enabled?')
print(f'{c[\"host\"]}:{c[\"port\"]}')")
  fi
  # Credentials come from the jolokia endpoint — passed via env or prompted.
  local user="${JOLOKIA_USER:?JOLOKIA_USER must be set}"
  local pass="${JOLOKIA_PASSWORD:?JOLOKIA_PASSWORD must be set}"
  printf 'https://%s:%s@%s' "$user" "$pass" "$_JOLOKIA_HOST"
}

# Log index pattern. Defaults to all daily indices (logs-*) so queries don't
# silently miss data when today's index doesn't exist yet (log shipping lags
# behind UTC midnight). Pin a single day with LOG_DATE=YYYY-MM-DD if desired.
# Results are sorted by timestamp desc, so the newest matching docs surface
# regardless of which daily index they live in.
log_index() {
  if [[ -n "${LOG_DATE:-}" ]]; then
    printf 'logs-%s' "$LOG_DATE"
  else
    printf 'logs-*'
  fi
}

# os_search <body-json> — raw OpenSearch _search response.
# ignore_unavailable/allow_no_indices keep wildcard queries from erroring when
# some daily index is missing.
os_search() {
  curl -s "$(os_uri)/$(log_index)/_search?ignore_unavailable=true&allow_no_indices=true" \
    -H 'Content-Type: application/json' -d "$1"
}

# svc_filter [service]  — JSON clause matching all log docs for a Kafka service.
# Logs carry a `service_name` field equal to the service (e.g. "k-aws-2"), while
# HOSTNAME is per-node ("k-aws-2-5"). Match on service_name so a query for one
# service never bleeds into another. Defaults to $SERVICE.
svc_filter() {
  local svc="${1:-$SERVICE}"
  printf '{"match_phrase":{"service_name":"%s"}}' "$svc"
}

# svc_search <message-query-string> [size]
# Convenience: all docs for $SERVICE whose MESSAGE matches a query_string.
# Pass an empty query to match every line for the service.
svc_search() {
  local q="$1" size="${2:-10}"
  local must="$(svc_filter)"
  if [[ -n "$q" ]]; then
    must="$must,{\"query_string\":{\"default_field\":\"MESSAGE\",\"query\":\"$q\"}}"
  fi
  os_search "{
    \"size\": $size,
    \"query\":{\"bool\":{\"must\":[$must]}},
    \"sort\":[{\"timestamp\":\"desc\"}],
    \"_source\":[\"timestamp\",\"HOSTNAME\",\"MESSAGE\"]
  }"
}

# host_msg_search <service> <phrase> [size]
# Searches MESSAGE for an exact phrase within a service's logs.
host_msg_search() {
  local svc="$1" phrase="$2" size="${3:-10}"
  os_search "{
    \"size\": $size,
    \"query\":{\"bool\":{\"must\":[
      $(svc_filter "$svc"),
      {\"match_phrase\":{\"MESSAGE\":\"$phrase\"}}
    ]}},
    \"sort\":[{\"timestamp\":\"desc\"}],
    \"_source\":[\"timestamp\",\"HOSTNAME\",\"MESSAGE\"]
  }"
}

# print_hits [truncate-length]
# Pretty-prints OpenSearch _search hits from stdin: timestamp | HOSTNAME :: MESSAGE
print_hits() {
  local trunc="${1:-150}"
  python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'error' in d:
    print('  query error:', json.dumps(d['error'])[:200]); sys.exit(0)
hits = d.get('hits', {}).get('hits', [])
print('hits:', len(hits))
for h in hits:
    s = h['_source']
    print(s['timestamp'], '|', s.get('HOSTNAME', '-'), '::', s['MESSAGE'][:$trunc])
"
}

# thanos_query <promql>  — instant query, returns raw JSON.
thanos_query() {
  curl -s --get "$(thanos_uri)/api/v1/query" --data-urlencode "query=$1"
}

# thanos_fmt [divisor]
# Formats thanos_query output from stdin: label  value  (divided by divisor, default 1).
thanos_fmt() {
  local div="${1:-1}"
  python3 -c "
import sys, json
div = float('$div')
d = json.load(sys.stdin)
if d.get('status') != 'success':
    print('  ERR', str(d)[:160]); sys.exit()
res = d['data']['result']
if not res:
    print('  (no data)'); sys.exit()
for r in res:
    m = r['metric']; v = float(r['value'][1])
    lbl = (m.get('service', '') + '/' + m.get('host', '')).strip('/') or m.get('topic', 'agg')
    print(f'  {lbl:30} {v/div:,.2f}')
"
}

# q <promql> [divisor]  — query Thanos and format output.
q() { thanos_query "$1" | thanos_fmt "${2:-1}"; }
