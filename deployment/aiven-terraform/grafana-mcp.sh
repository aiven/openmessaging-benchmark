#!/usr/bin/env bash
# Start the Grafana MCP server for the Grafana of an aiven-terraform environment.
# Usage: grafana-mcp.sh <env> [mcp-grafana options]
# Register it with: claude mcp add grafana-omb-<env> -- $PWD/grafana-mcp.sh <env>
set -euo pipefail

env_name=${1:?usage: grafana-mcp.sh <env> [mcp-grafana options]}
shift
dir=$(cd "$(dirname "$0")" && pwd)

tf_output() {
  TF_WORKSPACE=$env_name terraform -chdir="$dir" output -raw "$1"
}

GRAFANA_URL=$(tf_output grafana_service_uri)
GRAFANA_USERNAME=$(tf_output grafana_username)
GRAFANA_PASSWORD=$(tf_output grafana_password)
export GRAFANA_URL GRAFANA_USERNAME GRAFANA_PASSWORD

exec uvx mcp-grafana --disable-write "$@"
