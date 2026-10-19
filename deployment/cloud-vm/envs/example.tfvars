# Copy to envs/<name>.tfvars (ignored by git) and fill in. Run with: make provision ENV=<name>
public_key_path = "~/.ssh/kafka_aws.pub"
region          = "eu-central-1"
ami             = "ami-03fdcc940f72bc806" # Amazon-linux-arm64
assume_role     = "arn:aws:iam::123:role/test"
resource_prefix = "proj1"
resource_tags = {
  owner = "user@aiven.io"
}

# Use spot instances or fixed instances
use_spot_instance = false

# Instance types for the worker nodes and the monitoring node
worker_instance_type     = "m8g.xlarge"
monitoring_instance_type = "m8g.medium"

# Number of worker instances. By default half are producers and half are consumers.
# For a larger fanout (using -x on the OMB CLI), increase it, e.g. to 9 to have 2/3 as
# consumers (6) and the rest as producers.
worker_instance_count = 6

# Spread workers across zones or pin all nodes to a single zone
# az_override = "eu-central-1a"
az_override = "" # empty uses all zones

# Restrict the zones by zone ID. An empty list allows all zones.
# allowed_zone_ids = ["euc1-az1", "euc1-az2"]
allowed_zone_ids = []
