-- Recreate Network Cost Tables with Parquet Format
-- Run these statements in Athena to migrate from NDJSON to Parquet
--
-- IMPORTANT: This will drop existing tables. Historical NDJSON data will
-- become inaccessible. Back up if needed before running.
--
-- Replace <BUCKET_NAME> with your actual S3 bucket name.

-- Create database if not exists
CREATE DATABASE IF NOT EXISTS network_costs;

-- Drop existing JSON-backed tables
DROP TABLE IF EXISTS network_costs.network_cost_details;
DROP TABLE IF EXISTS network_costs.network_cost_summary;

-- Create details table (Parquet format)
CREATE EXTERNAL TABLE network_costs.network_cost_details (
    period_start          STRING,
    destination_category  STRING,
    local_ip              STRING,
    local_az              STRING,
    local_vpc_id          STRING,
    local_subnet_id       STRING,
    local_instance_id     STRING,
    local_region          STRING,
    remote_ip             STRING,
    remote_az             STRING,
    remote_vpc_id         STRING,
    remote_subnet_id      STRING,
    remote_instance_id    STRING,
    remote_region         STRING,
    local_pod_name        STRING,
    local_pod_namespace   STRING,
    local_service_name    STRING,
    remote_pod_name       STRING,
    remote_pod_namespace  STRING,
    remote_service_name   STRING,
    snat_ip               STRING,
    dnat_ip               STRING,
    target_port           INT,
    traversed_constructs  STRING,
    bytes                 BIGINT,
    gb                    DOUBLE,
    rate_per_gb           DOUBLE,
    estimated_cost_usd    DOUBLE
)
PARTITIONED BY (date STRING, hour STRING)
STORED AS PARQUET
LOCATION 's3://<BUCKET_NAME>/network-cost-data/details/'
TBLPROPERTIES (
    'parquet.compression'='SNAPPY',
    'classification'='parquet'
);

-- Create summary table (Parquet format)
CREATE EXTERNAL TABLE network_costs.network_cost_summary (
    period_start          STRING,
    namespace             STRING,
    destination_category  STRING,
    total_bytes           BIGINT,
    total_gb              DOUBLE,
    estimated_cost_usd    DOUBLE
)
PARTITIONED BY (date STRING, hour STRING)
STORED AS PARQUET
LOCATION 's3://<BUCKET_NAME>/network-cost-data/summary/'
TBLPROPERTIES (
    'parquet.compression'='SNAPPY',
    'classification'='parquet'
);

-- After running the Lambda with Parquet output, load new partitions:
-- MSCK REPAIR TABLE network_costs.network_cost_details;
-- MSCK REPAIR TABLE network_costs.network_cost_summary;
