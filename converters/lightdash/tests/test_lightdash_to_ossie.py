# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import json

from ossie import OssieDataType, OssieDialect

from ossie_lightdash import ConverterIssueType, LightdashToOssieConverter

SCHEMA_YML = {
    "version": 2,
    "models": [
        {
            "name": "orders",
            "description": "One row per order",
            "meta": {
                "primary_key": "order_id",
                "ai_hint": ["Orders placed in the web shop.", "One row per order."],
                "joins": [
                    {
                        "join": "customers",
                        "sql_on": "${orders.customer_id} = ${customers.customer_id}",
                    }
                ],
                "metrics": {
                    "conversion_rate": {
                        "type": "number",
                        "label": "Conversion rate",
                        "format": "percent",
                        "round": 1,
                        "sql": "SUM(${TABLE}.completed_count) / NULLIF(SUM(${TABLE}.total_count), 0)",
                    }
                },
            },
            "columns": [
                {
                    "name": "order_date",
                    "description": "Date the order was placed",
                    "meta": {"dimension": {"label": "Order date", "type": "date"}},
                },
                {
                    "name": "status",
                    "meta": {
                        "dimension": {
                            "label": "Status",
                            "type": "string",
                            "ai_hint": "Order lifecycle stage.",
                        }
                    },
                },
                {
                    "name": "updated_at",
                    "meta": {
                        "dimension": {"type": "timestamp", "time_intervals": "OFF"}
                    },
                },
                {
                    "name": "shipped_at",
                    "meta": {
                        "dimension": {"type": "timestamp", "time_intervals": ["DAY", "MONTH"]}
                    },
                },
                {
                    "name": "amount",
                    "description": "Order amount",
                    "meta": {
                        "dimension": {"type": "number"},
                        "metrics": {
                            "total_amount": {
                                "type": "sum",
                                "label": "Total amount",
                                "format": "usd",
                                "ai_hint": "Revenue before refunds.",
                            },
                            "latest_amount": {"type": "max"},
                            "median_amount": {"type": "median"},
                            "p90_amount": {"type": "percentile", "percentile": 90},
                        }
                    },
                },
                {"name": "completed_count"},
                {"name": "total_count"},
                {
                    "name": "customer_id",
                    "meta": {
                        "metrics": {
                            "unique_customers": {"type": "count_distinct"},
                        }
                    },
                },
            ],
        },
        {
            "name": "customers",
            "meta": {"primary_key": ["customer_id", "region"]},
            "columns": [{"name": "customer_id"}],
        },
    ],
}


def _metric(document, name):
    return next(m for m in document.semantic_model[0].metrics if m.name == name)


def _lightdash_data(element):
    for extension in element.custom_extensions or []:
        if extension.vendor_name == "lightdash":
            return json.loads(extension.data)
    return {}


class TestLightdashToOssie:
    def test_dataset_source_is_qualified(self):
        result = LightdashToOssieConverter().convert(
            SCHEMA_YML, database="analytics_db", schema="marts"
        )
        dataset = result.output.semantic_model[0].datasets[0]
        assert dataset.source == "analytics_db.marts.orders"
        assert not any(
            issue.issue_type is ConverterIssueType.SOURCE_UNQUALIFIED
            for issue in result.issues
        )

    def test_missing_schema_is_reported(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML)
        dataset = result.output.semantic_model[0].datasets[0]
        assert dataset.source == "orders"
        assert any(
            issue.issue_type is ConverterIssueType.SOURCE_UNQUALIFIED
            for issue in result.issues
        )

    def test_time_dimension(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        field = result.output.semantic_model[0].datasets[0].fields[0]
        assert field.name == "order_date"
        assert field.label == "Order date"
        assert field.description == "Date the order was placed"
        assert field.dimension is not None
        # The Lightdash type becomes a datatype; `is_time` is an Ossie role
        # marker with no Lightdash source, so it stays unset.
        assert field.datatype is OssieDataType.DATE
        assert field.dimension.is_time is None
        withdrawn = result.output.semantic_model[0].datasets[0].fields[2]
        assert withdrawn.name == "updated_at"
        assert withdrawn.dimension.is_time is False
        assert _lightdash_data(withdrawn) == {"type": "timestamp"}
        # A custom interval list is not a role marker: it stays in the extension.
        custom = result.output.semantic_model[0].datasets[0].fields[3]
        assert custom.dimension.is_time is None
        assert _lightdash_data(custom) == {"type": "timestamp", "time_intervals": ["DAY", "MONTH"]}

    def test_dimension_types_become_datatypes(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        by_name = {
            field.name: field
            for field in result.output.semantic_model[0].datasets[0].fields
        }
        assert by_name["status"].datatype is OssieDataType.STRING
        assert by_name["order_date"].datatype is OssieDataType.DATE

    def test_typed_metric_becomes_aggregation_expression(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        metric = _metric(result.output, "total_amount")
        assert metric.expression.dialects[0].expression == "SUM(orders.amount)"
        assert _lightdash_data(metric) == {"label": "Total amount", "format": "usd"}
        assert metric.ai_context == "Revenue before refunds."

    def test_count_distinct_metric(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        metric = _metric(result.output, "unique_customers")
        assert (
            metric.expression.dialects[0].expression
            == "COUNT(DISTINCT orders.customer_id)"
        )

    def test_percentile_metric_becomes_percentile_cont(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        metric = _metric(result.output, "p90_amount")
        assert (
            metric.expression.dialects[0].expression
            == "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.amount)"
        )
        assert _lightdash_data(metric) == {}

    def test_sql_metric_expression_is_rewritten(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        metric = _metric(result.output, "conversion_rate")
        assert (
            metric.expression.dialects[0].expression
            == "SUM(orders.completed_count) / NULLIF(SUM(orders.total_count), 0)"
        )
        assert _lightdash_data(metric) == {
            "label": "Conversion rate",
            "format": "percent",
            "round": 1,
        }

    def test_join_becomes_relationship(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        relationship = result.output.semantic_model[0].relationships[0]
        assert relationship.from_dataset == "orders"
        assert relationship.to == "customers"
        assert relationship.from_columns == ["customer_id"]
        assert relationship.to_columns == ["customer_id"]

    def test_percentile_with_sql_orders_by_the_expression(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "metrics": {
                            "p90_custom": {
                                "type": "percentile",
                                "percentile": 90,
                                "sql": "${TABLE}.amount - ${TABLE}.discount",
                            }
                        }
                    },
                    "columns": [],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        metric = _metric(result.output, "p90_custom")
        assert (
            metric.expression.dialects[0].expression
            == "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.amount - orders.discount)"
        )
        assert _lightdash_data(metric) == {}

    def test_joined_table_references_become_cross_dataset(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "metrics": {
                            "orders_per_customer": {
                                "type": "number",
                                "sql": "COUNT(${TABLE}.order_id) / COUNT(DISTINCT ${customers.customer_id})",
                            }
                        }
                    },
                    "columns": [],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        metric = _metric(result.output, "orders_per_customer")
        assert (
            metric.expression.dialects[0].expression
            == "COUNT(orders.order_id) / COUNT(DISTINCT customers.customer_id)"
        )

    def test_model_metric_without_sql_is_skipped(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {"metrics": {"broken_metric": {"type": "number"}}},
                    "columns": [],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        assert result.output.semantic_model[0].metrics is None
        assert any(
            issue.issue_type is ConverterIssueType.METRIC_SQL_MISSING
            and issue.element_name == "broken_metric"
            for issue in result.issues
        )

    def test_unparseable_join_is_reported(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "joins": [{"join": "customers", "sql_on": "1 = 1"}],
                    },
                    "columns": [],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        assert result.output.semantic_model[0].relationships is None
        assert any(
            issue.issue_type is ConverterIssueType.JOIN_SQL_UNPARSED
            for issue in result.issues
        )

    def test_typed_metric_with_sql_aggregates_the_expression(self):
        schema_yml = {
            "models": [
                {
                    "name": "work_orders",
                    "columns": [
                        {
                            "name": "status",
                            "meta": {
                                "metrics": {
                                    "completion_rate": {
                                        "type": "average",
                                        "sql": "CASE WHEN ${status} = 'Completed' THEN 1 ELSE 0 END",
                                    },
                                    "distinct_total": {"type": "sum_distinct"},
                                }
                            },
                        }
                    ],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        assert (
            _metric(result.output, "completion_rate").expression.dialects[0].expression
            == "AVG(CASE WHEN work_orders.status = 'Completed' THEN 1 ELSE 0 END)"
        )
        distinct_total = _metric(result.output, "distinct_total")
        assert (
            distinct_total.expression.dialects[0].expression
            == "SUM(DISTINCT work_orders.status)"
        )
        assert _lightdash_data(distinct_total) == {}

    def test_metric_reference_is_inlined(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "metrics": {
                            "amount_per_customer": {
                                "type": "number",
                                "sql": "${total_amount} / NULLIF(${unique_customers}, 0)",
                            }
                        }
                    },
                    "columns": [
                        {"name": "amount", "meta": {"metrics": {"total_amount": {"type": "sum"}}}},
                        {
                            "name": "customer_id",
                            "meta": {"metrics": {"unique_customers": {"type": "count_distinct"}}},
                        },
                    ],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        assert (
            _metric(result.output, "amount_per_customer").expression.dialects[0].expression
            == "(SUM(orders.amount)) / NULLIF((COUNT(DISTINCT orders.customer_id)), 0)"
        )
        assert [
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.METRIC_REFERENCE_INLINED
        ] == ["amount_per_customer", "amount_per_customer"]

    def test_bare_field_references_resolve_to_the_dataset(self):
        schema_yml = {
            "models": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "first_name", "meta": {"dimension": {"type": "string"}}},
                        {
                            "name": "full_name",
                            "meta": {
                                "dimension": {
                                    "type": "string",
                                    "sql": "${first_name} || ' ' || ${TABLE}.last_name",
                                }
                            },
                        },
                        {
                            "name": "order_count",
                            "meta": {
                                "dimension": {
                                    "type": "number",
                                    "sql": "(SELECT COUNT(*) FROM orders WHERE orders.customer_id = ${TABLE}.customer_id)",
                                }
                            },
                        },
                    ],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        by_name = {
            field.name: field.expression.dialects[0].expression
            for field in result.output.semantic_model[0].datasets[0].fields
        }
        assert by_name["full_name"] == "customers.first_name || ' ' || customers.last_name"
        assert by_name["order_count"] == (
            "(SELECT COUNT(*) FROM orders WHERE orders.customer_id = customers.customer_id)"
        )

    def test_parameter_references_skip_the_element(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "metrics": {
                            "my_orders": {
                                "type": "number",
                                "sql": "SUM(CASE WHEN ${TABLE}.owner = ${ld.user.email} THEN 1 END)",
                            }
                        }
                    },
                    "columns": [
                        {
                            "name": "is_recent",
                            "meta": {
                                "dimension": {
                                    "type": "boolean",
                                    "sql": "${TABLE}.order_date >= ${lightdash.parameters.start_date}",
                                }
                            },
                        },
                        {
                            "name": "status_label",
                            "meta": {
                                "dimension": {
                                    "type": "string",
                                    "sql": "{% if ld.query.filters contains 'orders.status' %} 'filtered' {% else %} ${TABLE}.status {% endif %}",
                                }
                            },
                        },
                        {"name": "order_date", "meta": {"dimension": {"type": "date"}}},
                    ],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        dataset = result.output.semantic_model[0].datasets[0]
        assert [field.name for field in dataset.fields] == ["order_date"]
        assert result.output.semantic_model[0].metrics is None
        assert sorted(
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.EXPRESSION_NOT_PORTABLE
        ) == ["is_recent", "my_orders", "status_label"]

    def test_aliased_joins_become_relationships(self):
        schema_yml = {
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "joins": [
                            {
                                "join": "date_dim",
                                "alias": "sold_date",
                                "sql_on": "${orders.sold_date_id} = ${sold_date.date_id}",
                                "relationship": "many-to-one",
                            },
                            {
                                "join": "date_dim",
                                "alias": "return_date",
                                "sql_on": "${orders.return_date_id} = ${return_date.date_id}",
                            },
                        ]
                    },
                    "columns": [
                        {
                            "name": "sold_year",
                            "meta": {"dimension": {"type": "number", "sql": "${sold_date.year}"}},
                        }
                    ],
                }
            ]
        }
        result = LightdashToOssieConverter().convert(schema_yml, schema="marts")
        relationships = result.output.semantic_model[0].relationships
        assert [(r.name, r.to, r.from_columns, r.to_columns) for r in relationships] == [
            ("orders_to_sold_date", "date_dim", ["sold_date_id"], ["date_id"]),
            ("orders_to_return_date", "date_dim", ["return_date_id"], ["date_id"]),
        ]
        assert _lightdash_data(relationships[0]) == {
            "alias": "sold_date",
            "relationship": "many-to-one",
        }
        field = result.output.semantic_model[0].datasets[0].fields[0]
        assert field.expression.dialects[0].expression == "date_dim.year"
        assert any(
            issue.issue_type is ConverterIssueType.ALIAS_REFERENCE_FLATTENED
            and issue.element_name == "sold_year"
            for issue in result.issues
        )

    def test_expressions_carry_the_warehouse_dialect(self):
        result = LightdashToOssieConverter(OssieDialect.BIGQUERY).convert(
            SCHEMA_YML, schema="marts"
        )
        metric = _metric(result.output, "total_amount")
        assert [d.dialect for d in metric.expression.dialects] == [OssieDialect.BIGQUERY]
        field = result.output.semantic_model[0].datasets[0].fields[0]
        assert field.expression.dialects[0].dialect is OssieDialect.BIGQUERY

    def test_primary_key_and_ai_hint_become_dataset_attributes(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        orders, customers = result.output.semantic_model[0].datasets
        assert orders.primary_key == ["order_id"]
        assert orders.ai_context == "Orders placed in the web shop.\nOne row per order."
        assert customers.primary_key == ["customer_id", "region"]
        status = next(field for field in orders.fields if field.name == "status")
        assert status.ai_context == "Order lifecycle stage."
        assert _lightdash_data(status) == {"type": "string"}

    def test_metric_datatypes_follow_the_aggregation(self):
        result = LightdashToOssieConverter().convert(SCHEMA_YML, schema="marts")
        assert _metric(result.output, "unique_customers").datatype is OssieDataType.INTEGER
        assert _metric(result.output, "total_amount").datatype is OssieDataType.DECIMAL
        assert _metric(result.output, "latest_amount").datatype is OssieDataType.DECIMAL
        assert _metric(result.output, "conversion_rate").datatype is None
