data "aiven_project" "project" {
  project = var.project
}

output "kafka_service_name" {
  value = aiven_kafka.kafka.service_name
}

output "kafka_service_uri" {
  description = "bootstrap.servers"
  value       = aiven_kafka.kafka.service_uri
  sensitive   = true
}

output "kafka_ca_cert" {
  value     = data.aiven_project.project.ca_cert
  sensitive = true
}

output "kafka_access_cert" {
  value     = aiven_kafka.kafka.kafka[0].access_cert
  sensitive = true
}

output "kafka_access_key" {
  value     = aiven_kafka.kafka.kafka[0].access_key
  sensitive = true
}

output "thanos_service_uri" {
  value     = aiven_thanos.metrics.service_uri
  sensitive = true
}

output "grafana_service_uri" {
  value     = aiven_grafana.dashboards.service_uri
  sensitive = true
}

output "grafana_username" {
  value = aiven_grafana.dashboards.service_username
}

output "grafana_password" {
  value     = aiven_grafana.dashboards.service_password
  sensitive = true
}

output "cloud_name" {
  value = aiven_kafka.kafka.cloud_name
}

output "thanos_remote_write_uri" {
  value     = aiven_thanos.metrics.thanos[0].receiver_remote_write_uri
  sensitive = true
}

output "thanos_username" {
  value = aiven_thanos.metrics.service_username
}

output "thanos_password" {
  value     = aiven_thanos.metrics.service_password
  sensitive = true
}
