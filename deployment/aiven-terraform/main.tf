resource "aiven_kafka" "kafka" {
  project      = var.project
  cloud_name   = var.cloud
  plan         = var.kafka_plan
  service_name = "${var.name_prefix}-kafka"

  lifecycle {
    precondition {
      condition     = var.diskless_enabled || length(var.auto_diskless_topic_regexes) == 0
      error_message = "auto_diskless_topic_regexes needs diskless_enabled = true. Otherwise Terraform creates regular topics."
    }
  }

  kafka_user_config {
    kafka_version = var.kafka_version

    dynamic "inkless" {
      for_each = var.inkless_enabled ? [1] : []
      content {
        enabled = true
      }
    }

    dynamic "kafka_diskless" {
      for_each = var.diskless_enabled ? [1] : []
      content {
        enabled                     = true
        auto_diskless_topic_regexes = length(var.auto_diskless_topic_regexes) > 0 ? var.auto_diskless_topic_regexes : null
      }
    }

    dynamic "tiered_storage" {
      for_each = var.tiered_storage_enabled ? [1] : []
      content {
        enabled = true
      }
    }
  }
}

resource "aiven_thanos" "metrics" {
  project      = var.project
  cloud_name   = var.cloud
  plan         = var.thanos_plan
  service_name = "${var.name_prefix}-thanos"
}

resource "aiven_grafana" "dashboards" {
  project      = var.project
  cloud_name   = var.cloud
  plan         = var.grafana_plan
  service_name = "${var.name_prefix}-grafana"
}

resource "aiven_service_integration" "kafka_metrics" {
  project                  = var.project
  integration_type         = "metrics"
  source_service_name      = aiven_kafka.kafka.service_name
  destination_service_name = aiven_thanos.metrics.service_name
}

resource "aiven_service_integration" "thanos_grafana" {
  project                  = var.project
  integration_type         = "datasource"
  source_service_name      = aiven_grafana.dashboards.service_name
  destination_service_name = aiven_thanos.metrics.service_name
}

resource "aiven_service_integration" "thanos_grafana_dashboard" {
  project                  = var.project
  integration_type         = "dashboard"
  source_service_name      = aiven_grafana.dashboards.service_name
  destination_service_name = aiven_thanos.metrics.service_name

  depends_on = [aiven_service_integration.kafka_metrics]
}
