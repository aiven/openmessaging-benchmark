# AWS module

[Example variables](../../envs/example.tfvars)

## Features

### Fixed vs Spot Instances

Select whether to use fixed-price instances or spot instances for cost savings:

```hcl
use_spot_instance = false
```

Use cheaper instances at the trade-off of potential interruptions.

### Multi-zone vs Single-zone Deployment

Choose to deploy instances across multiple availability zones for higher availability or within a single zone:

```hcl
az_override = "eu-west-1a"
```

Terraform will assign the zone ID on the output Ansible inventory file.

When `az_override` is empty, Terraform uses the zones that offer both `worker_instance_type` and `monitoring_instance_type`. To restrict the zones further, set `allowed_zone_ids`:

```hcl
allowed_zone_ids = ["euc1-az1", "euc1-az2"]
```

A zone ID names the same physical location in every AWS account. A zone name, for example `eu-central-1a`, can map to a different location in each account.
If no zone remains, `terraform plan` fails. When `az_override` is set, Terraform ignores `allowed_zone_ids` and uses the zone as given.
The filter calls `ec2:DescribeInstanceTypeOfferings`. The role in `assume_role` needs this permission.

