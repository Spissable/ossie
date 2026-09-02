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
"""Convert Lightdash semantic definitions into an Ossie document.

The input is a dbt ``schema.yml``-shaped dictionary whose ``meta`` blocks
carry Lightdash dimensions, metrics and joins. Structural information becomes
first-class Ossie vocabulary (datasets, fields, metrics, relationships);
Lightdash presentation attributes without Ossie vocabulary (``format``,
``round``, ``group_label``, ``hidden``, ...) are preserved in
``custom_extensions`` entries with ``vendor_name: "lightdash"`` so that the
export direction can reproduce them exactly.
"""

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

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

from ossie_lightdash.converter_issues import (
    ConverterIssue,
    ConverterIssueType,
    ConverterResult,
)
from ossie_lightdash.datatype_utils import lightdash_type_to_datatype, metric_datatype
from ossie_lightdash.expression_utils import (
    AGGREGATE_TYPES,
    build_aggregation,
    has_non_portable_reference,
    lightdash_sql_to_ossie,
)

LIGHTDASH_VENDOR_NAME = "lightdash"

# Keys that are structurally encoded in Ossie vocabulary and therefore must NOT
# be duplicated into the extension (a stale copy would win on export).
_STRUCTURAL_METRIC_KEYS = {"sql", "description", "ai_hint", "name"}
_STRUCTURAL_DIMENSION_KEYS = {"label", "sql", "ai_hint"}
_STRUCTURAL_JOIN_KEYS = {"join", "sql_on"}

_JOIN_PAIR_RE = re.compile(
    r"\$\{(\w+)\.(\w+)\}\s*=\s*\$\{(\w+)\.(\w+)\}",
)


def _expression(expression: str, dialect: OssieDialect) -> OssieExpression:
    return OssieExpression(
        dialects=[OssieDialectExpression(dialect=dialect, expression=expression)]
    )


def _lightdash_extension(data: Dict[str, Any]) -> List[OssieCustomExtension]:
    if not data:
        return []
    return [
        OssieCustomExtension(
            vendor_name=LIGHTDASH_VENDOR_NAME,
            data=json.dumps(data, ensure_ascii=False, sort_keys=True),
        )
    ]


def _ai_context(ai_hint: Any) -> Optional[str]:
    """Lightdash `ai_hint` (a string or a list of strings) as Ossie `ai_context`."""
    if isinstance(ai_hint, list):
        return "\n".join(str(hint) for hint in ai_hint) or None
    if isinstance(ai_hint, str):
        return ai_hint or None
    return None


def _primary_key(primary_key: Any) -> Optional[List[str]]:
    if isinstance(primary_key, str):
        return [primary_key]
    if isinstance(primary_key, list) and primary_key:
        return [str(column) for column in primary_key]
    return None


def _merge_meta(base: Any, override: Any) -> Dict[str, Any]:
    """Deep-merge two meta blocks; keys in ``override`` win."""
    merged: Dict[str, Any] = dict(base) if isinstance(base, dict) else {}
    if isinstance(override, dict):
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge_meta(merged[key], value)
            else:
                merged[key] = value
    return merged


def lightdash_meta(node: Dict[str, Any]) -> Dict[str, Any]:
    """The Lightdash meta of a dbt model or column.

    dbt 1.10+ moved ``meta`` under ``config``; Lightdash reads both and lets
    ``config.meta`` win, so the converter merges them the same way.
    """
    return _merge_meta(node.get("meta"), (node.get("config") or {}).get("meta"))


def _unique_name(base: str, used: Set[str]) -> str:
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


class _ModelContext:
    """Expression rewriting for one model: alias resolution, metric inlining
    and non-portable reference detection, with the issues they raise."""

    def __init__(
        self,
        dataset_name: str,
        aliases: Dict[str, str],
        definitions: Dict[str, Tuple[Dict[str, Any], Optional[str]]],
        issues: List[ConverterIssue],
    ) -> None:
        self.dataset_name = dataset_name
        self.aliases = aliases
        self.definitions = definitions
        self.issues = issues
        self.column_types: Dict[str, str] = {}
        self._expressions: Dict[str, Optional[str]] = {}
        self._resolving: Set[str] = set()

    def rewrite(self, sql: str, element_name: str) -> Optional[str]:
        """Rewrite Lightdash SQL into an Ossie expression, or None (with an
        issue) when it references parameters or user attributes."""
        if has_non_portable_reference(sql):
            self.issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.EXPRESSION_NOT_PORTABLE,
                    element_name=element_name,
                )
            )
            return None
        result = lightdash_sql_to_ossie(
            sql,
            self.dataset_name,
            aliases=self.aliases,
            resolve_metric=self.metric_expression,
        )
        for _ in result.inlined_metrics:
            self.issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.METRIC_REFERENCE_INLINED,
                    element_name=element_name,
                )
            )
        for _ in result.flattened_aliases:
            self.issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.ALIAS_REFERENCE_FLATTENED,
                    element_name=element_name,
                )
            )
        return result.expression

    def metric_expression(self, name: str) -> Optional[str]:
        """The Ossie expression of one of this model's metrics, or None when the
        name is not a metric, the metric is not portable, or it references
        itself."""
        if name not in self.definitions:
            return None
        if name in self._expressions:
            return self._expressions[name]
        if name in self._resolving:
            return None
        self._resolving.add(name)
        definition, column = self.definitions[name]
        expression = self._build_expression(name, definition, column)
        self._resolving.discard(name)
        self._expressions[name] = expression
        return expression

    def _build_expression(
        self, name: str, definition: Dict[str, Any], column: Optional[str]
    ) -> Optional[str]:
        sql = definition.get("sql")
        if sql:
            inner = self.rewrite(sql, name)
            if inner is None:
                return None
        elif column is not None:
            inner = f"{self.dataset_name}.{column}"
        else:
            return None
        lightdash_type = definition.get("type", "number")
        return (
            build_aggregation(lightdash_type, inner, definition.get("percentile"))
            or inner
        )


class LightdashToOssieConverter:
    """Converts a Lightdash-flavoured dbt schema.yml dict into an OssieDocument.

    Lightdash SQL is written for the project's warehouse; ``dialect`` labels the
    emitted expressions accordingly (``ANSI_SQL`` when the warehouse has no
    Ossie dialect, e.g. Postgres or Redshift).
    """

    def __init__(self, dialect: OssieDialect = OssieDialect.ANSI_SQL) -> None:
        self._dialect = dialect

    def convert(
        self,
        schema_yml: Dict[str, Any],
        *,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        semantic_model_name: str = "lightdash_semantic_model",
    ) -> ConverterResult[OssieDocument]:
        issues: List[ConverterIssue] = []
        datasets: List[OssieDataset] = []
        metrics: List[OssieMetric] = []
        relationships: List[OssieRelationship] = []
        relationship_names: Set[str] = set()
        metric_names: Set[str] = set()

        for model in schema_yml.get("models") or []:
            dataset, model_metrics, model_relationships = self._convert_model(
                model,
                database=database,
                schema=schema,
                issues=issues,
                relationship_names=relationship_names,
                metric_names=metric_names,
            )
            datasets.append(dataset)
            metrics.extend(model_metrics)
            relationships.extend(model_relationships)

        document = OssieDocument(
            version="0.2.0.dev0",
            semantic_model=[
                OssieSemanticModel(
                    name=semantic_model_name,
                    datasets=datasets,
                    metrics=metrics or None,
                    relationships=relationships or None,
                )
            ],
        )
        return ConverterResult(output=document, issues=issues)

    def _convert_model(
        self,
        model: Dict[str, Any],
        *,
        database: Optional[str],
        schema: Optional[str],
        issues: List[ConverterIssue],
        relationship_names: Set[str],
        metric_names: Set[str],
    ) -> Tuple[OssieDataset, List[OssieMetric], List[OssieRelationship]]:
        name = model["name"]
        source = ".".join(part for part in [database, schema, name] if part)
        if schema is None:
            issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.SOURCE_UNQUALIFIED,
                    element_name=name,
                )
            )

        model_meta = lightdash_meta(model)
        joins = model_meta.get("joins") or []
        aliases = {
            join["alias"]: join["join"]
            for join in joins
            if join.get("alias") and join.get("join")
        }

        # Metric definitions are collected before any SQL is rewritten so that
        # `${metric}` references can be inlined.
        definitions: Dict[str, Tuple[Dict[str, Any], Optional[str]]] = {}
        for column in model.get("columns") or []:
            column_meta = lightdash_meta(column)
            for metric_name, definition in (column_meta.get("metrics") or {}).items():
                definitions[metric_name] = (definition, column["name"])
        for metric_name, definition in (model_meta.get("metrics") or {}).items():
            definitions[metric_name] = (definition, None)

        context = _ModelContext(name, aliases, definitions, issues)
        for column in model.get("columns") or []:
            dimension_meta = lightdash_meta(column).get("dimension") or {}
            if dimension_meta.get("type"):
                context.column_types[column["name"]] = dimension_meta["type"]

        fields: List[OssieField] = []
        for column in model.get("columns") or []:
            field = self._convert_column(column, context)
            if field is not None:
                fields.append(field)

        metrics: List[OssieMetric] = []
        for metric_name, (definition, column_name) in definitions.items():
            metric = self._convert_metric(
                metric_name, definition, column_name, context, metric_names
            )
            if metric is not None:
                metrics.append(metric)

        relationships = self._convert_joins(
            joins,
            from_model=name,
            issues=issues,
            relationship_names=relationship_names,
        )

        dataset = OssieDataset(
            name=name,
            source=source,
            description=model.get("description"),
            primary_key=_primary_key(model_meta.get("primary_key")),
            ai_context=_ai_context(model_meta.get("ai_hint")),
            fields=fields or None,
        )
        return dataset, metrics, relationships

    def _convert_column(
        self, column: Dict[str, Any], context: _ModelContext
    ) -> Optional[OssieField]:
        column_name = column["name"]
        dimension_meta = lightdash_meta(column).get("dimension")

        expression = column_name
        dimension: Optional[OssieDimension] = None
        datatype = None
        label: Optional[str] = None
        ai_context: Optional[str] = None
        extension_data: Dict[str, Any] = {}
        if dimension_meta is not None:
            label = dimension_meta.get("label")
            ai_context = _ai_context(dimension_meta.get("ai_hint"))
            if dimension_meta.get("sql"):
                rewritten = context.rewrite(dimension_meta["sql"], column_name)
                if rewritten is None:
                    return None
                expression = rewritten
            datatype = lightdash_type_to_datatype(dimension_meta.get("type"))
            # `is_time` is a role marker in Ossie, not a type. Lightdash's only
            # role marker is `time_intervals: OFF`, which withdraws a temporal
            # column from the time axis; otherwise `is_time` is left unset so
            # the datatype decides.
            excluded = set(_STRUCTURAL_DIMENSION_KEYS)
            time_intervals = dimension_meta.get("time_intervals")
            if time_intervals is False or time_intervals == "OFF":
                dimension = OssieDimension(is_time=False)
                excluded.add("time_intervals")
            else:
                dimension = OssieDimension()
            extension_data = {
                key: value
                for key, value in dimension_meta.items()
                if key not in excluded
            }

        return OssieField(
            name=column_name,
            expression=_expression(expression, self._dialect),
            dimension=dimension,
            datatype=datatype,
            label=label,
            description=column.get("description"),
            ai_context=ai_context,
            custom_extensions=_lightdash_extension(extension_data) or None,
        )

    def _convert_metric(
        self,
        metric_name: str,
        definition: Dict[str, Any],
        column: Optional[str],
        context: _ModelContext,
        metric_names: Set[str],
    ) -> Optional[OssieMetric]:
        if column is None and not definition.get("sql"):
            context.issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.METRIC_SQL_MISSING,
                    element_name=metric_name,
                )
            )
            return None
        expression = context.metric_expression(metric_name)
        if expression is None:
            return None

        # Typed aggregations (and their percentile) are recovered from the
        # expression on export; only types an expression cannot encode
        # (`boolean`, `string`, `date`, ...) travel in the extension.
        lightdash_type = definition.get("type", "number")
        excluded = set(_STRUCTURAL_METRIC_KEYS)
        if lightdash_type == "number" or lightdash_type in AGGREGATE_TYPES:
            excluded.add("type")
        if lightdash_type == "percentile":
            excluded.add("percentile")
        extension_data = {
            key: value for key, value in definition.items() if key not in excluded
        }
        # Lightdash scopes metric names per model; Ossie scopes them per
        # semantic model. The Ossie name is Lightdash's own field id,
        # `<model>_<metric>`, and the bare name travels in the extension so
        # export restores it exactly.
        extension_data["name"] = metric_name
        qualified = f"{context.dataset_name}_{metric_name}"
        ossie_name = _unique_name(qualified, metric_names)
        if ossie_name != qualified:
            context.issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.METRIC_NAME_COLLISION,
                    element_name=metric_name,
                )
            )
        return OssieMetric(
            name=ossie_name,
            expression=_expression(expression, self._dialect),
            datatype=metric_datatype(
                lightdash_type,
                context.column_types.get(column) if not definition.get("sql") else None,
            ),
            description=definition.get("description"),
            ai_context=_ai_context(definition.get("ai_hint")),
            custom_extensions=_lightdash_extension(extension_data) or None,
        )

    @staticmethod
    def _convert_joins(
        joins: List[Dict[str, Any]],
        *,
        from_model: str,
        issues: List[ConverterIssue],
        relationship_names: Set[str],
    ) -> List[OssieRelationship]:
        relationships: List[OssieRelationship] = []
        for join in joins:
            to_model = join.get("join")
            # An aliased join is referenced by its alias in `sql_on`.
            reference = join.get("alias") or to_model
            pairs = _JOIN_PAIR_RE.findall(join.get("sql_on") or "")
            from_columns: List[str] = []
            to_columns: List[str] = []
            for left_table, left_column, right_table, right_column in pairs:
                if left_table == from_model and right_table == reference:
                    from_columns.append(left_column)
                    to_columns.append(right_column)
                elif left_table == reference and right_table == from_model:
                    from_columns.append(right_column)
                    to_columns.append(left_column)
            if not to_model or not from_columns:
                issues.append(
                    ConverterIssue(
                        issue_type=ConverterIssueType.JOIN_SQL_UNPARSED,
                        element_name=f"{from_model} -> {to_model or '<unknown>'}",
                    )
                )
                continue
            # The alias and any other join attributes (type, relationship,
            # fields, ...) have no Ossie vocabulary and travel in the extension.
            extras = {
                key: value
                for key, value in join.items()
                if key not in _STRUCTURAL_JOIN_KEYS
            }
            relationships.append(
                OssieRelationship.model_validate(
                    {
                        "name": _unique_name(
                            f"{from_model}_to_{reference}", relationship_names
                        ),
                        "from": from_model,
                        "to": to_model,
                        "from_columns": from_columns,
                        "to_columns": to_columns,
                        "custom_extensions": _lightdash_extension(extras) or None,
                    }
                )
            )
        return relationships
