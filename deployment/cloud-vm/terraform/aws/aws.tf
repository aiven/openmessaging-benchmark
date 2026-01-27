provider "aws" {
  region = var.region
  assume_role {
    role_arn = var.assume_role
  }
  default_tags {
    tags = var.resource_tags
  }
}

provider "random" {
}

variable "assume_role" {}

resource "random_id" "hash" {
  byte_length = 8
}

variable "region" {}

variable "ami" {}

variable "username" {
  type    = string
  default = "ec2-user"
}

variable "public_key_path" {
  description = <<DESCRIPTION
Path to the SSH public key to be used for authentication.
Ensure this keypair is added to your local SSH agent so provisioners can
connect.

Example: ~/.ssh/omb-aws.pub
DESCRIPTION
}

variable "key_name" {
  default     = "omb-key"
  description = "Desired name prefix for the AWS key pair"
}

variable "resource_tags" {
  description = "Additional resource tags"
  type        = map(string)
  default = {
  }
}

variable "resource_prefix" {
  type    = string
  default = ""
}

variable "worker_instance_type" {
  type    = string
  default = "m8g.large"
}

variable "monitoring_instance_type" {
  type    = string
  default = "m8g.medium"
}

variable "instance_state" {
  type    = string
  default = "running"
}

variable "monitoring_instance_state" {
  type    = string
  default = "running"
}

variable "worker_instance_count" {
  type    = number
  default = 2
}

variable "use_spot_instance" {
  description = "Whether to use a Spot instance instead of an On-Demand instance"
  type        = bool
  default     = false
}

variable "az_override" {
  description = "Specific AZ to use if not using all"
  type        = string
  default     = ""
}

locals {
  use_all_azs = var.az_override == ""
}

data "aws_availability_zone" "pinned_az" {
  count = local.use_all_azs ? 0 : 1
  name  = var.az_override
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # if not pinned to a single-az, then use all available azs; otherwise, use single az
  selected_azs = local.use_all_azs ? data.aws_availability_zones.available.names : data.aws_availability_zone.pinned_az.*.name
}

# Create a VPC to launch our instances into
resource "aws_vpc" "benchmark_vpc" {
  cidr_block = "10.0.0.0/16"

  tags = {
    Name = "${var.resource_prefix}omb-vpc-${random_id.hash.hex}"
  }
}

# Create an internet gateway to give our subnet access to the outside world
resource "aws_internet_gateway" "kafka" {
  vpc_id = aws_vpc.benchmark_vpc.id
}

# Grant the VPC internet access on its main route table
resource "aws_route" "internet_access" {
  route_table_id         = aws_vpc.benchmark_vpc.main_route_table_id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.kafka.id
}

# Create a subnet to launch our instances into
resource "aws_subnet" "benchmark_subnet" {
  count                   = length(local.selected_azs)
  vpc_id                  = aws_vpc.benchmark_vpc.id
  cidr_block              = cidrsubnet(aws_vpc.benchmark_vpc.cidr_block, 8, count.index)
  map_public_ip_on_launch = true
  availability_zone       = element(local.selected_azs, count.index)

  tags = {
    Name = "${var.resource_prefix}omb-subnet-${random_id.hash.hex}"
  }
}

resource "aws_security_group" "benchmark_security_group" {
  name   = "omb-${random_id.hash.hex}"
  vpc_id = aws_vpc.benchmark_vpc.id

  # SSH access from anywhere
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Monitoring port from anywhere
  ## JMX agent port
  ingress {
    from_port   = 7000
    to_port     = 7000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ## Prometheus port
  ingress {
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ## Grafana port
  ingress {
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # All ports open within the VPC
  ingress {
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  # outbound internet access
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.resource_prefix}omb-sec-group-${random_id.hash.hex}"
  }
}

resource "aws_key_pair" "auth" {
  key_name   = "${var.key_name}-${random_id.hash.hex}"
  public_key = file(var.public_key_path)
}

resource "aws_spot_instance_request" "worker" {
  ami                    = var.ami
  instance_type          = var.worker_instance_type
  key_name               = aws_key_pair.auth.id
  subnet_id              = element(aws_subnet.benchmark_subnet[*].id, count.index)
  vpc_security_group_ids = [aws_security_group.benchmark_security_group.id]
  count                  = var.use_spot_instance ? var.worker_instance_count : 0
  availability_zone      = element(local.selected_azs, count.index)

  tags = {
    Name = "${var.resource_prefix}omb-worker-spot-${count.index}"
  }

  wait_for_fulfillment = true
  spot_type            = "one-time"
}

resource "aws_instance" "worker" {
  ami                    = var.ami
  instance_type          = var.worker_instance_type
  key_name               = aws_key_pair.auth.id
  subnet_id              = element(aws_subnet.benchmark_subnet[*].id, count.index)
  vpc_security_group_ids = [aws_security_group.benchmark_security_group.id]
  count                  = var.use_spot_instance ? 0 : var.worker_instance_count
  availability_zone      = element(local.selected_azs, count.index)

  tags = {
    Name = "${var.resource_prefix}omb-worker-${count.index}"
  }
}

resource "aws_instance" "monitoring" {
  ami                    = var.ami
  instance_type          = var.monitoring_instance_type
  key_name               = aws_key_pair.auth.id
  subnet_id              = element(aws_subnet.benchmark_subnet[*].id, count.index)
  vpc_security_group_ids = [aws_security_group.benchmark_security_group.id]
  count                  = 1

  # 80gb local storage
  root_block_device {
    volume_size = 80
    volume_type = "gp2"
  }

  tags = {
    Name = "${var.resource_prefix}omb-monitoring-${count.index}"
  }
}

# Create a local map to convert AZ names to zone IDs
locals {
  az_to_zone_id = {
    for az in data.aws_availability_zones.available.names :
    az => data.aws_availability_zones.available.zone_ids[index(data.aws_availability_zones.available.names, az)]
  }
}

resource "local_file" "inventory" {
  content = templatefile("${path.module}/../inventory.tmpl",
    {
      number_of_workers  = range(var.worker_instance_count),
      worker_ips         = var.use_spot_instance ? aws_spot_instance_request.worker.*.public_ip : aws_instance.worker.*.public_ip,
      worker_private_ips = var.use_spot_instance ? aws_spot_instance_request.worker.*.private_ip : aws_instance.worker.*.private_ip,
      worker_azs         = var.use_spot_instance ? aws_spot_instance_request.worker.*.availability_zone : aws_instance.worker.*.availability_zone,
      worker_custom_azs = [
        for c in var.use_spot_instance ? aws_spot_instance_request.worker : aws_instance.worker :
        local.az_to_zone_id[c.availability_zone]
      ]
      monitoring_ip         = aws_instance.monitoring[0].public_ip,
      monitoring_private_ip = aws_instance.monitoring[0].private_ip,
      username              = var.username,
      public_key_path       = var.public_key_path,
    }
  )
  filename = "hosts.yaml"
}

resource "aws_ec2_instance_state" "worker" {
  for_each    = { for idx, instance in aws_instance.worker : idx => instance }
  instance_id = each.value.id
  state       = var.instance_state
}

resource "aws_ec2_instance_state" "monitoring" {
  for_each    = { for idx, instance in aws_instance.monitoring : idx => instance }
  instance_id = each.value.id
  state       = var.monitoring_instance_state
}

output "worker_ssh_host" {
  value = var.use_spot_instance ? aws_spot_instance_request.worker[0].public_ip : aws_instance.worker[0].public_ip
}

output "monitoring_ssh_host" {
  value = aws_instance.monitoring[0].public_ip
}

output "username" {
  value = var.username
}

output "public_key_path" {
  value = var.public_key_path
}
