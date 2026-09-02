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

from ossie import (
    OssieCustomExtension,
    OssieDataset,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieMetric,
    OssieRelationship,
    OssieSemanticModel,
)

from ossie_lightdash import ConverterIssueType, OssieToLightdashConverter


def _ansi(expression: str) -> OssieExpression:
    return OssieExpression(
        dialects=[
            OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=expression)
        ]
    )


def _document() -> OssieDocument:
    orders = OssieDataset(
        name="orders",
        source="analytics_db.marts.orders",
        description="One row per order",
        fields=[
            OssieField(
                name="order_date",
                expression=_ansi("order_date"),
                dimension=OssieDimension(is_time=True),
                label="Order date",
            ),
            OssieField(
                name="status",
                expression=_ansi("status"),
                dimension=OssieDimension(is_time=False),
            ),
            OssieField(name="amount", expression=_ansi("amount")),
            OssieField(name="customer_id", expression=_ansi("customer_id")),
        ],
    )
    customers = OssieDataset(
        name="customers",
        source="analytics_db.marts.customers",
        fields=[OssieField(name="customer_id", expression=_ansi("customer_id"))],
    )
    metrics = [
        OssieMetric(
            name="total_amount",
            expression=_ansi("SUM(orders.amount)"),
            description="Sum of order amounts",
            custom_extensions=[
                OssieCustomExtension(
                    vendor_name="lightdash",
                    data=json.dumps({"label": "Total amount", "format": "usd"}),
                )
            ],
        ),
        OssieMetric(
            name="conversion_rate",
            expression=_ansi(
                "SUM(orders.completed_count) / NULLIF(SUM(orders.total_count), 0)"
            ),
            custom_extensions=[
                OssieCustomExtension(
                    vendor_name="lightdash",
                    data=json.dumps({"format": "percent", "round": 1}),
                )
            ],
        ),
        OssieMetric(
            name="cross_dataset",
            expression=_ansi("SUM(orders.amount) / COUNT(customers.customer_id)"),
        ),
        OssieMetric(
            name="foreign_vendor_metric",
            expression=_ansi("SUM(orders.amount)"),
            custom_extensions=[
                OssieCustomExtension(vendor_name="somebi", data='{"x": 1}')
            ],
        ),
    ]
    relationships = [
        OssieRelationship.model_validate(
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            }
        )
    ]
    return OssieDocument(
        version="0.2.0.dev0",
        semantic_model=[
            OssieSemanticModel(
                name="sales",
                datasets=[orders, customers],
                metrics=metrics,
                relationships=relationships,
            )
        ],
    )


def _model(output, name):
    return next(m for m in output["models"] if m["name"] == name)


def _column(model, name):
    return next(c for c in model["columns"] if c["name"] == name)


class TestOssieToLightdash:
    def test_time_dimension_exports_date_type(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "order_date")
        assert column["meta"]["dimension"] == {"label": "Order date", "type": "date"}

    def test_categorical_dimension_keeps_dimension_marker(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "status")
        assert column["meta"]["dimension"] == {}

    def test_plain_field_has_no_dimension_meta(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "amount")
        assert "dimension" not in column.get("meta", {})

    def test_simple_aggregation_becomes_column_metric(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "amount")
        metric = column["meta"]["metrics"]["total_amount"]
        assert metric["type"] == "sum"
        assert metric["label"] == "Total amount"
        assert metric["format"] == "usd"
        assert metric["description"] == "Sum of order amounts"
        assert "sql" not in metric

    def test_complex_expression_becomes_model_metric(self):
        result = OssieToLightdashConverter().convert(_document())
        metric = _model(result.output, "orders")["meta"]["metrics"]["conversion_rate"]
        assert metric["type"] == "number"
        assert (
            metric["sql"]
            == "SUM(${TABLE}.completed_count) / NULLIF(SUM(${TABLE}.total_count), 0)"
        )
        assert metric["format"] == "percent"
        assert metric["round"] == 1

    def test_cross_dataset_metric_is_dropped_with_issue(self):
        result = OssieToLightdashConverter().convert(_document())
        assert any(
            issue.issue_type is ConverterIssueType.CROSS_DATASET_METRIC_DROPPED
            and issue.element_name == "cross_dataset"
            for issue in result.issues
        )

    def test_foreign_extension_is_reported(self):
        result = OssieToLightdashConverter().convert(_document())
        assert any(
            issue.issue_type is ConverterIssueType.FOREIGN_EXTENSION_IGNORED
            and issue.element_name == "foreign_vendor_metric"
            for issue in result.issues
        )

    def test_extension_cannot_override_structural_keys(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        metric = tampered.semantic_model[0].metrics[0].model_copy(
            update={
                "custom_extensions": [
                    OssieCustomExtension(
                        vendor_name="lightdash",
                        data=json.dumps(
                            {"label": "Total amount", "sql": "1 + 1", "description": "stale"}
                        ),
                    )
                ]
            }
        )
        tampered.semantic_model[0].metrics[0] = metric
        result = OssieToLightdashConverter().convert(tampered)
        column = _column(_model(result.output, "orders"), "amount")
        exported = column["meta"]["metrics"]["total_amount"]
        assert exported["label"] == "Total amount"
        assert "sql" not in exported
        assert exported["description"] == "Sum of order amounts"

    def test_mismatched_relationship_columns_are_skipped(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        relationship = OssieRelationship.model_validate(
            {
                "name": "broken",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id", "order_id"],
                "to_columns": ["customer_id"],
            }
        )
        tampered.semantic_model[0].relationships[0] = relationship
        result = OssieToLightdashConverter().convert(tampered)
        assert "joins" not in _model(result.output, "orders").get("meta", {})
        assert any(
            issue.issue_type is ConverterIssueType.RELATIONSHIP_COLUMNS_MISMATCHED
            and issue.element_name == "broken"
            for issue in result.issues
        )

    def test_invalid_extension_json_is_reported(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        metric = tampered.semantic_model[0].metrics[0].model_copy(
            update={
                "custom_extensions": [
                    OssieCustomExtension(vendor_name="lightdash", data="{not json")
                ]
            }
        )
        tampered.semantic_model[0].metrics[0] = metric
        result = OssieToLightdashConverter().convert(tampered)
        assert any(
            issue.issue_type is ConverterIssueType.EXTENSION_DATA_INVALID
            and issue.element_name == "total_amount"
            for issue in result.issues
        )

    def test_relationship_becomes_join(self):
        result = OssieToLightdashConverter().convert(_document())
        joins = _model(result.output, "orders")["meta"]["joins"]
        assert joins == [
            {
                "join": "customers",
                "sql_on": "${orders.customer_id} = ${customers.customer_id}",
            }
        ]
