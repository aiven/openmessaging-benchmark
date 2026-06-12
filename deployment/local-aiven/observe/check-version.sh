#!/usr/bin/env bash
# Report the running Kafka commitId per node for a service.
#
# Usage:
#   SERVICE=k-aws-1 ./check-version.sh
#   SERVICE=k-aws-1 ./check-version.sh 5    # check 5 nodes
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/lib.sh"

NODES="${1:-3}"

echo "=== Kafka commitId per node ($SERVICE) ==="
for n in $(seq 1 "$NODES"); do
  host="${SERVICE}-${n}"
  host_msg_search "$host" 'Kafka commitId' 1 | python3 -c "
import sys, json
host = '$host'
d = json.load(sys.stdin)
if 'error' in d:
    print(host, ' query error:', json.dumps(d['error'])[:160]); sys.exit(0)
h = d['hits']['hits']
if h:
    m = h[0]['_source']['MESSAGE']; ts = h[0]['_source']['timestamp']
    cid = m.split('commitId:')[1].strip().split()[0] if 'commitId:' in m else m
    print(f'{host}  {ts}  {cid}')
else:
    print(f'{host}  <no commitId line found>')
"
done
