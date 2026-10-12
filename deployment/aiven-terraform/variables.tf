variable "aiven_api_token" {
  description = "Aiven API token. When null, the provider reads AIVEN_TOKEN."
  type        = string
  default     = null
  sensitive   = true
}

variable "project" {
  description = "Aiven project that holds the services."
  type        = string
}

variable "cloud" {
  description = "Cloud region, e.g. aws-eu-west-1."
  type        = string
}

variable "name_prefix" {
  description = "Prefix of the service names: <prefix>-kafka, <prefix>-thanos, <prefix>-grafana."
  type        = string
}

variable "kafka_plan" {
  type = string
}

variable "kafka_version" {
  description = "Kafka version. When null, the Aiven default is used."
  type        = string
  default     = null
}

variable "inkless_enabled" {
  description = "Enable the Inkless functionality on the Kafka service."
  type        = bool
  default     = false
}

variable "diskless_enabled" {
  description = "Enable the Diskless functionality on the Kafka service."
  type        = bool
  default     = false
}

variable "auto_diskless_topic_regexes" {
  description = "Topics matching any regex are created as diskless. Needs diskless_enabled."
  type        = list(string)
  default     = []
}

variable "tiered_storage_enabled" {
  type    = bool
  default = false
}

variable "thanos_plan" {
  type = string
}

variable "grafana_plan" {
  type = string
}
