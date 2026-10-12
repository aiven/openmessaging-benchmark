# Copy to envs/<name>.tfvars (ignored by git) and fill in. Run with: make plan ENV=<name>
# Token: export AIVEN_TOKEN, or TF_VAR_aiven_api_token, or add aiven_api_token to secrets.auto.tfvars.
project     = "my-project"
cloud       = "aws-eu-west-1"
name_prefix = "omb-dev"

kafka_plan   = "kafka-professional-3"
thanos_plan  = "startup-4"
grafana_plan = "startup-1"

# Optional
# kafka_version               = "4.0"
# inkless_enabled             = true
# diskless_enabled            = true
# auto_diskless_topic_regexes = ["omb-.*"]
# tiered_storage_enabled      = true
