# Token: set TF_VAR_aiven_api_token or AIVEN_TOKEN, or put aiven_api_token in an
# uncommitted secrets.auto.tfvars. The provider has no API URL argument (schema 4.64.0).
provider "aiven" {
  api_token = var.aiven_api_token
}
