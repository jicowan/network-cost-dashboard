"""
Network Cost Dashboard — Streamlit UI

Queries Athena tables populated by the network-cost-exporter Lambda
and displays per-namespace and per-workload network cost breakdowns.
"""

import os
import time
from datetime import date, timedelta

import boto3
import pandas as pd
import streamlit as st

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "default")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
ATHENA_OUTPUT = os.environ.get(
    "ATHENA_OUTPUT_BUCKET", f"s3://{S3_BUCKET}/athena-results/"
)
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

athena = boto3.client("athena", region_name=AWS_REGION)


# -------------------------------------------------------------------
# Athena query helpers
# -------------------------------------------------------------------

def run_query(sql):
    """Execute an Athena query and return a DataFrame."""
    resp = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    query_id = resp["QueryExecutionId"]

    # Poll until complete
    for _ in range(60):
        status = athena.get_query_execution(QueryExecutionId=query_id)
        state = status["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status["QueryExecution"]["Status"].get(
                "StateChangeReason", "Unknown"
            )
            st.error(f"Query {state}: {reason}")
            return pd.DataFrame()
        time.sleep(1)
    else:
        st.error("Query timed out")
        return pd.DataFrame()

    # Fetch results
    rows = []
    columns = []
    paginator = athena.get_paginator("get_query_results")
    first_page = True

    for page in paginator.paginate(QueryExecutionId=query_id):
        result_rows = page["ResultSet"]["Rows"]
        if first_page:
            columns = [
                col["VarCharValue"]
                for col in result_rows[0]["Data"]
            ]
            result_rows = result_rows[1:]
            first_page = False

        for row in result_rows:
            rows.append([
                col.get("VarCharValue", "") for col in row["Data"]
            ])

    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(rows, columns=columns)


# -------------------------------------------------------------------
# Page config
# -------------------------------------------------------------------

st.set_page_config(
    page_title="EKS Network Costs",
    page_icon=":cloud:",
    layout="wide",
)

st.title("EKS Network Cost Dashboard")

# -------------------------------------------------------------------
# Sidebar: filters
# -------------------------------------------------------------------

st.sidebar.header("Filters")

default_end = date.today()
default_start = default_end - timedelta(days=7)

date_range = st.sidebar.date_input(
    "Date range",
    value=(default_start, default_end),
)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date, end_date = default_start, default_end

start_str = start_date.strftime("%Y-%m-%d")
end_str = end_date.strftime("%Y-%m-%d")

# -------------------------------------------------------------------
# Tab layout
# -------------------------------------------------------------------

tab_overview, tab_namespaces, tab_flows, tab_query = st.tabs(
    ["Overview", "By Namespace", "Top Flows", "Custom Query"]
)

# -------------------------------------------------------------------
# Tab 1: Overview
# -------------------------------------------------------------------

with tab_overview:
    st.subheader("Cost by Traffic Category")

    df_cat = run_query(f"""
        SELECT
            destination_category,
            SUM(total_gb) AS total_gb,
            SUM(estimated_cost_usd) AS cost
        FROM network_cost_summary
        WHERE date BETWEEN '{start_str}' AND '{end_str}'
        GROUP BY destination_category
        ORDER BY cost DESC
    """)

    if not df_cat.empty:
        df_cat["total_gb"] = pd.to_numeric(df_cat["total_gb"])
        df_cat["cost"] = pd.to_numeric(df_cat["cost"])

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Data Transfer", f"{df_cat['total_gb'].sum():,.1f} GB")
        col2.metric("Estimated Cost", f"${df_cat['cost'].sum():,.2f}")
        col3.metric("Date Range", f"{start_str} to {end_str}")

        st.bar_chart(
            df_cat.set_index("destination_category")["cost"],
            horizontal=True,
        )
        st.dataframe(df_cat, use_container_width=True, hide_index=True)
    else:
        st.info("No data for the selected date range.")

    # Daily trend
    st.subheader("Daily Cost Trend")

    df_daily = run_query(f"""
        SELECT
            date,
            destination_category,
            SUM(estimated_cost_usd) AS cost
        FROM network_cost_summary
        WHERE date BETWEEN '{start_str}' AND '{end_str}'
        GROUP BY date, destination_category
        ORDER BY date
    """)

    if not df_daily.empty:
        df_daily["cost"] = pd.to_numeric(df_daily["cost"])
        pivot = df_daily.pivot(
            index="date",
            columns="destination_category",
            values="cost",
        ).fillna(0)
        st.line_chart(pivot)

# -------------------------------------------------------------------
# Tab 2: By Namespace
# -------------------------------------------------------------------

with tab_namespaces:
    st.subheader("Cost by Namespace")

    df_ns = run_query(f"""
        SELECT
            namespace,
            destination_category,
            SUM(total_gb) AS total_gb,
            SUM(estimated_cost_usd) AS cost
        FROM network_cost_summary
        WHERE date BETWEEN '{start_str}' AND '{end_str}'
        GROUP BY namespace, destination_category
        ORDER BY cost DESC
    """)

    if not df_ns.empty:
        df_ns["total_gb"] = pd.to_numeric(df_ns["total_gb"])
        df_ns["cost"] = pd.to_numeric(df_ns["cost"])

        # Namespace total summary
        ns_totals = (
            df_ns.groupby("namespace")[["total_gb", "cost"]]
            .sum()
            .sort_values("cost", ascending=False)
        )
        st.bar_chart(ns_totals["cost"], horizontal=True)

        # Drill-down into a specific namespace
        selected_ns = st.selectbox(
            "Select namespace for breakdown",
            options=ns_totals.index.tolist(),
        )

        ns_detail = df_ns[df_ns["namespace"] == selected_ns]
        st.dataframe(ns_detail, use_container_width=True, hide_index=True)

        # Show workload-level detail for selected namespace
        st.subheader(f"Top Workloads in {selected_ns}")

        df_wl = run_query(f"""
            SELECT
                local_service_name,
                destination_category,
                SUM(gb) AS total_gb,
                SUM(estimated_cost_usd) AS cost
            FROM network_cost_details
            WHERE local_pod_namespace = '{selected_ns}'
              AND date BETWEEN '{start_str}' AND '{end_str}'
            GROUP BY local_service_name, destination_category
            ORDER BY cost DESC
            LIMIT 50
        """)

        if not df_wl.empty:
            df_wl["total_gb"] = pd.to_numeric(df_wl["total_gb"])
            df_wl["cost"] = pd.to_numeric(df_wl["cost"])
            st.dataframe(df_wl, use_container_width=True, hide_index=True)
    else:
        st.info("No data for the selected date range.")

# -------------------------------------------------------------------
# Tab 3: Top Flows
# -------------------------------------------------------------------

with tab_flows:
    st.subheader("Top Cross-AZ Flows")

    col_cat, col_limit = st.columns(2)

    with col_cat:
        category_filter = st.selectbox(
            "Traffic category",
            options=[
                "INTER_AZ",
                "INTER_VPC",
                "INTER_REGION",
                "UNCLASSIFIED",
                "AMAZON_S3",
                "AMAZON_DYNAMODB",
            ],
        )

    with col_limit:
        limit = st.slider("Number of flows", min_value=10, max_value=100, value=25)

    col_pod_only, col_aggregate = st.columns(2)

    with col_pod_only:
        pod_only = st.checkbox(
            "Show pod-to-pod flows only",
            value=True,
            help="Exclude node-level traffic (kubelet, kube-proxy, etc.)",
        )

    with col_aggregate:
        aggregate_by_service = st.checkbox(
            "Aggregate by service",
            value=True,
            help="Group by service name instead of individual pod IPs",
        )

    # Build WHERE clause
    where_clauses = [
        f"destination_category = '{category_filter}'",
        f"date BETWEEN '{start_str}' AND '{end_str}'",
    ]
    if pod_only:
        where_clauses.append("local_pod_namespace != ''")

    where_sql = " AND ".join(where_clauses)

    if aggregate_by_service:
        # Aggregate by service (no remote_ip)
        df_flows = run_query(f"""
            SELECT
                local_pod_namespace,
                local_service_name,
                local_az,
                remote_pod_namespace,
                remote_service_name,
                remote_az,
                target_port,
                SUM(gb) AS total_gb,
                SUM(estimated_cost_usd) AS cost
            FROM network_cost_details
            WHERE {where_sql}
            GROUP BY 1, 2, 3, 4, 5, 6, 7
            ORDER BY cost DESC
            LIMIT {limit}
        """)
    else:
        # Show individual pod IPs
        df_flows = run_query(f"""
            SELECT
                local_pod_namespace,
                local_service_name,
                local_az,
                remote_pod_namespace,
                remote_service_name,
                remote_az,
                remote_ip,
                target_port,
                SUM(gb) AS total_gb,
                SUM(estimated_cost_usd) AS cost
            FROM network_cost_details
            WHERE {where_sql}
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY cost DESC
            LIMIT {limit}
        """)

    if not df_flows.empty:
        df_flows["total_gb"] = pd.to_numeric(df_flows["total_gb"])
        df_flows["cost"] = pd.to_numeric(df_flows["cost"])

        col1, col2 = st.columns(2)
        col1.metric(
            f"Total {category_filter} Transfer",
            f"{df_flows['total_gb'].sum():,.1f} GB",
        )
        col2.metric(
            f"Total {category_filter} Cost",
            f"${df_flows['cost'].sum():,.2f}",
        )

        st.dataframe(df_flows, use_container_width=True, hide_index=True)
    else:
        st.info(f"No {category_filter} traffic for the selected date range.")

# -------------------------------------------------------------------
# Tab 4: Custom Query
# -------------------------------------------------------------------

with tab_query:
    st.subheader("Run a Custom Athena Query")

    st.markdown(
        "Available tables: `network_cost_details`, `network_cost_summary`. "
        "Both are partitioned by `date` and `hour`."
    )

    custom_sql = st.text_area(
        "SQL",
        value=f"""SELECT
    namespace,
    SUM(estimated_cost_usd) AS cost,
    SUM(total_gb) AS total_gb
FROM network_cost_summary
WHERE date BETWEEN '{start_str}' AND '{end_str}'
GROUP BY namespace
ORDER BY cost DESC""",
        height=200,
    )

    if st.button("Run Query"):
        with st.spinner("Running query..."):
            df_custom = run_query(custom_sql)
        if not df_custom.empty:
            st.dataframe(df_custom, use_container_width=True, hide_index=True)

            csv = df_custom.to_csv(index=False)
            st.download_button(
                "Download CSV",
                data=csv,
                file_name="network_costs_query.csv",
                mime="text/csv",
            )
        else:
            st.info("Query returned no results.")
