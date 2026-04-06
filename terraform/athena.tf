# -----------------------------------------------------------------------------
# Glue Database and Tables for Athena
# -----------------------------------------------------------------------------

resource "aws_glue_catalog_database" "network_costs" {
  name        = var.athena_database
  description = "Network cost data from EKS Network Flow Monitor"
}

# -----------------------------------------------------------------------------
# Details Table (individual flow records)
# -----------------------------------------------------------------------------

resource "aws_glue_catalog_table" "details" {
  name          = "network_cost_details"
  database_name = aws_glue_catalog_database.network_costs.name
  description   = "Detailed per-flow network cost records"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"        = "parquet"
    "parquet.compression"   = "SNAPPY"
    "projection.enabled"    = "true"
    "projection.date.type"  = "date"
    "projection.date.range" = "2024-01-01,NOW"
    "projection.date.format" = "yyyy-MM-dd"
    "projection.hour.type"  = "integer"
    "projection.hour.range" = "00,23"
    "projection.hour.digits" = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.cost_data.id}/${var.s3_prefix}/details/date=$${date}/hour=$${hour}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.cost_data.id}/${var.s3_prefix}/details/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "period_start"
      type = "string"
    }
    columns {
      name = "destination_category"
      type = "string"
    }
    columns {
      name    = "direction"
      type    = "string"
      comment = "egress (external) or internal (cluster)"
    }
    columns {
      name = "local_ip"
      type = "string"
    }
    columns {
      name = "local_az"
      type = "string"
    }
    columns {
      name = "local_vpc_id"
      type = "string"
    }
    columns {
      name = "local_subnet_id"
      type = "string"
    }
    columns {
      name = "local_instance_id"
      type = "string"
    }
    columns {
      name = "local_region"
      type = "string"
    }
    columns {
      name = "remote_ip"
      type = "string"
    }
    columns {
      name = "remote_az"
      type = "string"
    }
    columns {
      name = "remote_vpc_id"
      type = "string"
    }
    columns {
      name = "remote_subnet_id"
      type = "string"
    }
    columns {
      name = "remote_instance_id"
      type = "string"
    }
    columns {
      name = "remote_region"
      type = "string"
    }
    columns {
      name = "local_pod_name"
      type = "string"
    }
    columns {
      name = "local_pod_namespace"
      type = "string"
    }
    columns {
      name = "local_service_name"
      type = "string"
    }
    columns {
      name = "remote_pod_name"
      type = "string"
    }
    columns {
      name = "remote_pod_namespace"
      type = "string"
    }
    columns {
      name = "remote_service_name"
      type = "string"
    }
    columns {
      name = "snat_ip"
      type = "string"
    }
    columns {
      name = "dnat_ip"
      type = "string"
    }
    columns {
      name = "target_port"
      type = "int"
    }
    columns {
      name = "traversed_constructs"
      type = "string"
    }
    columns {
      name = "bytes"
      type = "bigint"
    }
    columns {
      name = "gb"
      type = "double"
    }
    columns {
      name = "rate_per_gb"
      type = "double"
    }
    columns {
      name = "estimated_cost_usd"
      type = "double"
    }
  }

  partition_keys {
    name = "date"
    type = "string"
  }
  partition_keys {
    name = "hour"
    type = "string"
  }
}

# -----------------------------------------------------------------------------
# Summary Table (namespace aggregates)
# -----------------------------------------------------------------------------

resource "aws_glue_catalog_table" "summary" {
  name          = "network_cost_summary"
  database_name = aws_glue_catalog_database.network_costs.name
  description   = "Namespace-level network cost summary"

  table_type = "EXTERNAL_TABLE"

  parameters = {
    "classification"        = "parquet"
    "parquet.compression"   = "SNAPPY"
    "projection.enabled"    = "true"
    "projection.date.type"  = "date"
    "projection.date.range" = "2024-01-01,NOW"
    "projection.date.format" = "yyyy-MM-dd"
    "projection.hour.type"  = "integer"
    "projection.hour.range" = "00,23"
    "projection.hour.digits" = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.cost_data.id}/${var.s3_prefix}/summary/date=$${date}/hour=$${hour}/"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.cost_data.id}/${var.s3_prefix}/summary/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "period_start"
      type = "string"
    }
    columns {
      name = "namespace"
      type = "string"
    }
    columns {
      name = "destination_category"
      type = "string"
    }
    columns {
      name = "total_bytes"
      type = "bigint"
    }
    columns {
      name = "total_gb"
      type = "double"
    }
    columns {
      name = "estimated_cost_usd"
      type = "double"
    }
  }

  partition_keys {
    name = "date"
    type = "string"
  }
  partition_keys {
    name = "hour"
    type = "string"
  }
}
