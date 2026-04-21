module "aws" {
  source = "./../terraform/aws"
  # vars
  public_key_path = "~/.ssh/kafka_aws.pub"
  region          = "us-east-1"
  ami             = "ami-0b11e0ed3f8697f97" // Amazon-linux-arm64
  assume_role     = "arn:aws:iam::450367589208:role/AivenDeveloperAccess"
  resource_prefix = "jeqo-v1"
  resource_tags = {
    aiven-project      = "jeqo-omb"
    aiven-service-name = "jeqo-v1-diskless"
    owner              = "jorge.quilcate@aiven.io"
  }

  use_spot_instance = false
  worker_instance_type = "m8g.2xlarge"
  monitoring_instance_type = "m8g.medium"
  worker_instance_count = 12
  allowed_zone_ids = ["use1-az1", "use1-az2", "use1-az5"]
}

output "worker_ssh_host" {
  value = module.aws.worker_ssh_host
}

output "monitoring_ssh_host" {
  value = module.aws.monitoring_ssh_host
}

output "username" {
  value = module.aws.username
}

output "public_key_path" {
  value = module.aws.public_key_path
}

