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
"""Convert an Ossie document into Lightdash semantic definitions.

The output is a dbt ``schema.yml``-shaped dictionary whose ``meta`` blocks
carry Lightdash dimensions, metrics and joins, ready to be merged into a dbt
project that Lightdash reads. Lightdash-specific presentation attributes that
have no Ossie vocabulary round-trip through ``custom_extensions`` entries with
``vendor_name: "lightdash"``; their keys are overlaid onto the generated
definitions and win for presentation attributes, while structural keys
(``sql``/``label`` on dimensions, ``sql``/``description`` on metrics,
``join``/``sql_on`` on joins) are protected so they can never override the
Ossie-derived definition.
"""

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from ossie import (
    OssieDataset,
    OssieDialect,
    OssieDocument,
    OssieExpression,
    OssieMetric,
    OssieSemanticModel,
)

from ossie_lightdash.converter_issues import (
    ConverterIssue,
    ConverterIssueType,
    ConverterResult,
)
from ossie_lightdash.datatype_utils import datatype_to_lightdash_type, is_temporal
from ossie_lightdash.expression_utils import (
    is_column_reference,
    ossie_sql_to_lightdash,
    parse_aggregation,
    qualifier_of,
    referenced_datasets,
    strip_qualifier,
)

LIGHTDASH_VENDOR_NAME = "lightdash"

# Structural keys are owned by Ossie vocabulary (the import direction never puts
# them into the extension); dropping them here keeps a hand-authored extension
# from overriding the Ossie-derived definition. ``type`` stays overridable on
# metrics whose expression is not a recognised aggregation: it is the channel
# for types an expression cannot express (``boolean``, ``string``, ...).
_PROTECTED_DIMENSION_KEYS = {"sql", "label", "ai_hint"}
_PROTECTED_METRIC_KEYS = {"sql", "description", "ai_hint", "name"}
_PROTECTED_AGGREGATION_KEYS = _PROTECTED_METRIC_KEYS | {"type", "percentile"}
_PROTECTED_JOIN_KEYS = {"join", "sql_on", "alias"}

# Joins a dataset declares, keyed by the joined dataset: the name Lightdash SQL
# uses to reference it (the joined model, or its alias). The first join to a
# dataset wins when it is joined more than once.
JoinReferences = Dict[str, str]


def _lightdash_extension_data(element: Any, issues: List[ConverterIssue]) -> Dict[str, Any]:
    """Return the ``lightdash`` vendor extension data of an Ossie element, if any."""
    data: Dict[str, Any] = {}
    for extension in element.custom_extensions or []:
        if extension.vendor_name == LIGHTDASH_VENDOR_NAME:
            try:
                data.update(json.loads(extension.data))
            except (TypeError, ValueError):
                issues.append(
                    ConverterIssue(
                        issue_type=ConverterIssueType.EXTENSION_DATA_INVALID,
                        element_name=getattr(element, "name", "<unnamed>"),
                    )
                )
        else:
            issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.FOREIGN_EXTENSION_IGNORED,
                    element_name=getattr(element, "name", "<unnamed>"),
                )
            )
    return data


def _ai_hint(ai_context: Any) -> Any:
    """Ossie `ai_context` as a Lightdash `ai_hint`: one line stays a string, a
    multi-line instruction becomes a list, and synonyms and examples of the
    structured form are rendered as extra hints."""
    if ai_context is None:
        return None
    if isinstance(ai_context, str):
        hints = ai_context.split("\n")
    else:
        hints = (ai_context.instructions or "").split("\n")
        if ai_context.synonyms:
            hints.append("Also known as: " + ", ".join(ai_context.synonyms))
        if ai_context.examples:
            hints.append("Example questions: " + "; ".join(ai_context.examples))
    hints = [hint for hint in hints if hint]
    if not hints:
        return None
    return hints[0] if len(hints) == 1 else hints


def _nest_meta_under_config(node: Dict[str, Any]) -> None:
    """Move a node's ``meta`` under ``config`` (the dbt 1.10+ placement)."""
    meta = node.pop("meta", None)
    if meta:
        node["config"] = {"meta": meta}


def _model_name_for(dataset: OssieDataset) -> str:
    """A Lightdash table is addressed by its dbt model name = the source's table part."""
    return dataset.source.rsplit(".", 1)[-1]


class OssieToLightdashConverter:
    """Converts an OssieDocument into a Lightdash-flavoured dbt schema.yml dict.

    ``dialect`` is the expression dialect to prefer (the project's warehouse);
    ``ANSI_SQL`` is the fallback, and an expression offering neither is taken
    from its first dialect with a ``DIALECT_UNAVAILABLE`` issue.
    """

    def __init__(
        self,
        dialect: OssieDialect = OssieDialect.ANSI_SQL,
        *,
        meta_under_config: bool = False,
    ) -> None:
        self._dialect = dialect
        self._meta_under_config = meta_under_config

    def convert(self, document: OssieDocument) -> ConverterResult[Dict[str, Any]]:
        issues: List[ConverterIssue] = []
        models: List[Dict[str, Any]] = []
        for semantic_model in document.semantic_model:
            models.extend(self._convert_semantic_model(semantic_model, issues))
        if self._meta_under_config:
            for model in models:
                _nest_meta_under_config(model)
                for column in model.get("columns") or []:
                    _nest_meta_under_config(column)
        return ConverterResult(output={"version": 2, "models": models}, issues=issues)

    def _pick_expression(
        self, expression: OssieExpression, element_name: str, issues: List[ConverterIssue]
    ) -> str:
        by_dialect = {
            dialect_expression.dialect: dialect_expression.expression
            for dialect_expression in expression.dialects
        }
        for dialect in (self._dialect, OssieDialect.ANSI_SQL):
            if dialect in by_dialect:
                return by_dialect[dialect]
        if not expression.dialects:
            return ""
        issues.append(
            ConverterIssue(
                issue_type=ConverterIssueType.DIALECT_UNAVAILABLE,
                element_name=element_name,
            )
        )
        return expression.dialects[0].expression

    def _convert_semantic_model(
        self, semantic_model: OssieSemanticModel, issues: List[ConverterIssue]
    ) -> List[Dict[str, Any]]:
        datasets = semantic_model.datasets or []
        dataset_names = {dataset.name for dataset in datasets}
        model_name_by_dataset = {
            dataset.name: _model_name_for(dataset) for dataset in datasets
        }

        # Joins are planned first: field and metric expressions may reference
        # other datasets only through the joins their own dataset declares.
        joins_by_dataset, references_by_dataset = self._plan_joins(
            semantic_model, model_name_by_dataset, issues
        )

        models_by_dataset: Dict[str, Dict[str, Any]] = {}
        columns_by_dataset: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for dataset in datasets:
            model, columns = self._convert_dataset(
                dataset, dataset_names, references_by_dataset.get(dataset.name, {}), issues
            )
            models_by_dataset[dataset.name] = model
            columns_by_dataset[dataset.name] = columns

        for metric in semantic_model.metrics or []:
            self._convert_metric(
                metric,
                [dataset.name for dataset in datasets],
                models_by_dataset,
                columns_by_dataset,
                references_by_dataset,
                issues,
            )

        for dataset_name, joins in joins_by_dataset.items():
            models_by_dataset[dataset_name].setdefault("meta", {})["joins"] = joins

        return [models_by_dataset[dataset.name] for dataset in datasets]

    def _plan_joins(
        self,
        semantic_model: OssieSemanticModel,
        model_name_by_dataset: Dict[str, str],
        issues: List[ConverterIssue],
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, JoinReferences]]:
        joins_by_dataset: Dict[str, List[Dict[str, Any]]] = {}
        references_by_dataset: Dict[str, JoinReferences] = {}
        joined_pairs: Set[Tuple[str, str]] = set()
        for relationship in semantic_model.relationships or []:
            from_model_name = model_name_by_dataset.get(relationship.from_dataset)
            to_model_name = model_name_by_dataset.get(relationship.to)
            if from_model_name is None or to_model_name is None:
                continue
            if len(relationship.from_columns) != len(relationship.to_columns):
                issues.append(
                    ConverterIssue(
                        issue_type=ConverterIssueType.RELATIONSHIP_COLUMNS_MISMATCHED,
                        element_name=relationship.name,
                    )
                )
                continue
            extension_data = _lightdash_extension_data(relationship, issues)
            # Lightdash refuses to join the same table twice without an alias;
            # a stashed alias is restored, otherwise the relationship name
            # aliases every repeat of a dataset pair.
            pair = (relationship.from_dataset, relationship.to)
            alias = extension_data.get("alias")
            if alias is None and pair in joined_pairs:
                alias = relationship.name
            joined_pairs.add(pair)
            join_reference = alias or to_model_name
            sql_on = " AND ".join(
                f"${{{from_model_name}.{from_column}}} = ${{{join_reference}.{to_column}}}"
                for from_column, to_column in zip(
                    relationship.from_columns, relationship.to_columns
                )
            )
            # An Ossie relationship always runs from the many side to the one
            # side; a stashed Lightdash `relationship` overrides it below.
            join: Dict[str, Any] = {"join": to_model_name}
            if alias:
                join["alias"] = alias
            join["sql_on"] = sql_on
            join["relationship"] = "many-to-one"
            join.update(
                {
                    key: value
                    for key, value in extension_data.items()
                    if key not in _PROTECTED_JOIN_KEYS
                }
            )
            joins_by_dataset.setdefault(relationship.from_dataset, []).append(join)
            references_by_dataset.setdefault(relationship.from_dataset, {}).setdefault(
                relationship.to, join_reference
            )
        return joins_by_dataset, references_by_dataset

    def _convert_dataset(
        self,
        dataset: OssieDataset,
        dataset_names: Set[str],
        references: JoinReferences,
        issues: List[ConverterIssue],
    ) -> tuple:
        columns_by_name: Dict[str, Dict[str, Any]] = {}
        for field in dataset.fields or []:
            column: Dict[str, Any] = {"name": field.name}
            if field.description:
                column["description"] = field.description

            dimension: Dict[str, Any] = {}
            ai_hint = _ai_hint(field.ai_context)
            if field.dimension is None and (field.label or ai_hint is not None):
                # Lightdash keeps labels and AI hints on dimensions only;
                # writing them would turn a measure-only field into one.
                issues.append(
                    ConverterIssue(
                        issue_type=ConverterIssueType.FIELD_ATTRIBUTE_NOT_REPRESENTABLE,
                        element_name=field.name,
                    )
                )
            elif field.dimension is not None:
                if field.label:
                    dimension["label"] = field.label
                if ai_hint is not None:
                    dimension["ai_hint"] = ai_hint
            if field.dimension is not None:
                # Only dimension fields carry a Lightdash type: emitting one for
                # a measure-only field would turn it into a dimension on import.
                lightdash_type = datatype_to_lightdash_type(field.datatype)
                if lightdash_type is not None:
                    dimension["type"] = lightdash_type
                elif field.dimension.is_time:
                    # No datatype to translate, but the field is declared as a
                    # time axis: `date` is the closest Lightdash type.
                    dimension["type"] = "date"
            if (
                field.dimension is not None
                and field.dimension.is_time
                and not is_temporal(field.datatype)
                and field.datatype is not None
            ):
                # A non-temporal datatype flagged as a time axis (e.g. a year
                # stored as Integer) has no Lightdash equivalent.
                issues.append(
                    ConverterIssue(
                        issue_type=ConverterIssueType.TIME_ROLE_NOT_REPRESENTABLE,
                        element_name=field.name,
                    )
                )
            if (
                field.dimension is not None
                and field.dimension.is_time is False
                and is_temporal(field.datatype)
            ):
                # Explicitly withdrawn from the time axis: Lightdash's role
                # marker for that is `time_intervals: OFF`.
                dimension["time_intervals"] = "OFF"
            expression = self._pick_expression(field.expression, field.name, issues)
            if expression and expression != field.name:
                unjoined = referenced_datasets(expression, dataset_names) - {
                    dataset.name,
                    *references,
                }
                if unjoined:
                    issues.append(
                        ConverterIssue(
                            issue_type=ConverterIssueType.FIELD_REFERENCE_UNJOINED,
                            element_name=field.name,
                        )
                    )
                dimension["sql"] = ossie_sql_to_lightdash(
                    expression, dataset.name, references
                )
            dimension.update(
                {
                    key: value
                    for key, value in _lightdash_extension_data(field, issues).items()
                    if key not in _PROTECTED_DIMENSION_KEYS
                }
            )
            # An empty dict still marks dimension-ness: a field Ossie declares as a
            # categorical dimension must not degrade to a plain column on export,
            # or the import direction could not reconstruct it.
            if dimension or field.dimension is not None:
                column["meta"] = {"dimension": dimension}
            columns_by_name[field.name] = column

        model: Dict[str, Any] = {"name": _model_name_for(dataset)}
        if dataset.description:
            model["description"] = dataset.description
        meta: Dict[str, Any] = {}
        if dataset.primary_key:
            keys = list(dataset.primary_key)
            meta["primary_key"] = keys[0] if len(keys) == 1 else keys
        ai_hint = _ai_hint(dataset.ai_context)
        if ai_hint is not None:
            meta["ai_hint"] = ai_hint
        if meta:
            model["meta"] = meta
        model["columns"] = list(columns_by_name.values())
        return model, columns_by_name

    def _convert_metric(
        self,
        metric: OssieMetric,
        dataset_names: List[str],
        models_by_dataset: Dict[str, Dict[str, Any]],
        columns_by_dataset: Dict[str, Dict[str, Dict[str, Any]]],
        references_by_dataset: Dict[str, JoinReferences],
        issues: List[ConverterIssue],
    ) -> None:
        expression = self._pick_expression(metric.expression, metric.name, issues)
        extension_data = _lightdash_extension_data(metric, issues)

        target_dataset = self._resolve_target_dataset(
            expression, dataset_names, references_by_dataset
        )
        if target_dataset is None:
            issues.append(
                ConverterIssue(
                    issue_type=ConverterIssueType.CROSS_DATASET_METRIC_DROPPED,
                    element_name=metric.name,
                )
            )
            return
        references = references_by_dataset.get(target_dataset, {})

        definition: Dict[str, Any] = {}
        if metric.description:
            definition["description"] = metric.description
        ai_hint = _ai_hint(metric.ai_context)
        if ai_hint is not None:
            definition["ai_hint"] = ai_hint

        # A single aggregation becomes a typed metric: on the column when the
        # operand is one of the dataset's columns, otherwise on the model with
        # the operand as `sql`. Anything else is a `number` metric with raw SQL.
        target_column: Optional[str] = None
        parsed = parse_aggregation(expression)
        if parsed is not None:
            definition["type"] = parsed.lightdash_type
            inner = parsed.inner
            if (
                is_column_reference(inner)
                and qualifier_of(inner) in (None, target_dataset)
                and strip_qualifier(inner) in columns_by_dataset[target_dataset]
            ):
                target_column = strip_qualifier(inner)
            else:
                definition["sql"] = ossie_sql_to_lightdash(inner, target_dataset, references)
            if parsed.percentile is not None:
                definition["percentile"] = parsed.percentile
            protected = _PROTECTED_AGGREGATION_KEYS
        else:
            definition["type"] = extension_data.get("type", "number")
            definition["sql"] = ossie_sql_to_lightdash(expression, target_dataset, references)
            protected = _PROTECTED_METRIC_KEYS

        definition.update(
            {
                key: value
                for key, value in extension_data.items()
                if key not in protected
            }
        )

        # The Lightdash name is the stashed one, else the Ossie name with the
        # `<model>_` field-id prefix removed, else the Ossie name as is.
        lightdash_name = extension_data.get("name")
        if not isinstance(lightdash_name, str) or not lightdash_name:
            prefix = f"{target_dataset}_"
            lightdash_name = (
                metric.name[len(prefix):]
                if metric.name.startswith(prefix) and len(metric.name) > len(prefix)
                else metric.name
            )

        if target_column is not None:
            column = columns_by_dataset[target_dataset][target_column]
            metrics = column.setdefault("meta", {}).setdefault("metrics", {})
            metrics[lightdash_name] = definition
        else:
            model = models_by_dataset[target_dataset]
            metrics = model.setdefault("meta", {}).setdefault("metrics", {})
            metrics[lightdash_name] = definition

    @staticmethod
    def _resolve_target_dataset(
        expression: str,
        dataset_names: List[str],
        references_by_dataset: Dict[str, JoinReferences],
    ) -> Optional[str]:
        """The dataset whose model hosts the metric.

        A metric spanning several datasets lives on the one that joins all the
        others directly: Lightdash resolves ``${other.column}`` only against
        the joins the hosting model declares, never transitively.
        """
        referenced = referenced_datasets(expression, set(dataset_names))
        if len(referenced) == 0:
            return dataset_names[0] if len(dataset_names) == 1 else None
        if len(referenced) == 1:
            return next(iter(referenced))
        for name in dataset_names:
            if name in referenced and referenced - {name} <= set(
                references_by_dataset.get(name, {})
            ):
                return name
        return None
