# AWS module

[Template](./module.template)

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

