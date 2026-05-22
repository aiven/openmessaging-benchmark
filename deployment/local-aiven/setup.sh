#!/usr/bin/env bash
set -euo pipefail

# Fetches Aiven Kafka credentials and renders ALL driver templates for local use.
#
# Required environment:
#   SERVICE  — Aiven Kafka service name
#
# Prereqs: avn CLI installed and authenticated (avn user login / profile configured)

SERVICE="${SERVICE:?SERVICE env var must be set to your Aiven Kafka service name}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATED_DIR="${SCRIPT_DIR}/generated"
DRIVERS_DIR="${SCRIPT_DIR}/drivers"

if ! command -v avn &>/dev/null; then
    echo "ERROR: 'avn' CLI not found. Install: pip install aiven-client" >&2
    exit 1
fi

if ! command -v jq &>/dev/null; then
    echo "ERROR: 'jq' not found. Install: brew install jq" >&2
    exit 1
fi

echo "==> Fetching service URI for '${SERVICE}'..."
SERVICE_URI=$(avn service get "${SERVICE}" --json | jq -r '.service_uri')

if [[ -z "${SERVICE_URI}" || "${SERVICE_URI}" == "null" ]]; then
    echo "ERROR: Could not retrieve service_uri for '${SERVICE}'" >&2
    exit 1
fi
echo "    bootstrap.servers: ${SERVICE_URI}"

echo "==> Downloading credentials for '${SERVICE}'..."
CREDS_TMP=$(mktemp -d)
trap 'rm -rf "${CREDS_TMP}"' EXIT

(cd "${CREDS_TMP}" && avn service user-creds-download "${SERVICE}" --username avnadmin)

# Validate downloaded files
for f in ca.pem service.cert service.key; do
    if [[ ! -f "${CREDS_TMP}/${f}" ]]; then
        echo "ERROR: Expected credential file not found: ${f}" >&2
        exit 1
    fi
done

echo "==> Generating configs in ${GENERATED_DIR}/"
mkdir -p "${GENERATED_DIR}"

# CA certificate
cp "${CREDS_TMP}/ca.pem" "${GENERATED_DIR}/${SERVICE}-ca.pem"

# Combined keystore (cert + key)
cat "${CREDS_TMP}/service.cert" "${CREDS_TMP}/service.key" > "${GENERATED_DIR}/${SERVICE}-service.pem"

# Render ALL driver templates
KEYSTORE_PATH="${GENERATED_DIR}/${SERVICE}-service.pem"
TRUSTSTORE_PATH="${GENERATED_DIR}/${SERVICE}-ca.pem"

for template in "${DRIVERS_DIR}"/*.yaml; do
    template_name=$(basename "${template}" .yaml)
    output="${GENERATED_DIR}/${SERVICE}-${template_name}.yaml"

    sed -e "s|{{ *svc_name *}}|${SERVICE}|g" \
        -e "s|{{ *kafka_service_uri *}}|${SERVICE_URI}|g" \
        -e "s|{{ *keystore_path *}}|${KEYSTORE_PATH}|g" \
        -e "s|{{ *truststore_path *}}|${TRUSTSTORE_PATH}|g" \
        "${template}" > "${output}"

    echo "    rendered: ${template_name} -> $(basename "${output}")"
done

echo ""
echo "Done. Generated files:"
ls -1 "${GENERATED_DIR}/"
echo ""
echo "Run benchmark with:"
echo "  make run SERVICE=${SERVICE} DRIVER=default"
echo "  make run SERVICE=${SERVICE} DRIVER=diskless"
echo "  make run SERVICE=${SERVICE} DRIVER=throughput"
