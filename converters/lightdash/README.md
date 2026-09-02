<!--
  Licensed to the Apache Software Foundation (ASF) under one
  or more contributor license agreements.  See the NOTICE file
  distributed with this work for additional information
  regarding copyright ownership.  The ASF licenses this file
  to you under the Apache License, Version 2.0 (the
  "License"); you may not use this file except in compliance
  with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing,
  software distributed under the License is distributed on an
  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
  KIND, either express or implied.  See the License for the
  specific language governing permissions and limitations
  under the License.
-->

# Apache Ossie <> Lightdash converter

Bidirectional converter between Ossie documents and
[Lightdash](https://github.com/lightdash/lightdash) semantic definitions.
Lightdash reads its semantic layer from dbt `schema.yml` files: dimensions and
metrics are declared per column (and per model) under `meta`. This converter
translates between that shape and Ossie.

- **Export** (`ossie_to_lightdash`): Ossie document → a dbt `schema.yml`-shaped
  dictionary with Lightdash `meta` blocks, ready to merge into a dbt project.
- **Import** (`lightdash_to_ossie`): a Lightdash-flavoured `schema.yml` → an
  Ossie document, as a migration path for teams with an existing installed
  base of Lightdash metrics.

```
ossie-lightdash export semantic_model.yaml schema.yml --dialect BIGQUERY
ossie-lightdash import schema.yml semantic_model.json --database analytics_db --schema marts --dialect BIGQUERY
```

## Mapping

| Ossie | Lightdash (dbt meta) |
| ----- | -------------------- |
| `dataset` | dbt model (`name` = table part of `source`) |
| `dataset.source` | assembled on import from `--database` / `--schema` / model name |
| `dataset.primary_key` | model `meta.primary_key` (a single key as a string, a composite key as a list) |
| `ai_context` on datasets, fields and metrics | `ai_hint` on models, dimensions and metrics; a multi-line instruction is a list of hints, and the synonyms / examples of the structured form are rendered as extra hints on export |
| `field` (no `dimension`) | plain column entry |
| `field` with `dimension` | `columns[].meta.dimension` (an empty `dimension: {}` marks a dimension with no extra attributes) |
| `field.datatype` | `meta.dimension.type` (`String`→`string`, `Integer`/`Decimal`/`Float`→`number`, `Date`→`date`, `DateTime`/`DateTimeTz`→`timestamp`, `Boolean`→`boolean`, `Time`/`Opaque`→`string`); on import `number` maps back to `Decimal` |
| `field.dimension.is_time` | `time_intervals: OFF` ↔ an explicit `is_time: false` on a temporal column; otherwise not carried — a non-temporal time axis (e.g. a year stored as `Integer`) has no Lightdash equivalent |
| `field.label` / `.description` | `meta.dimension.label` / column `description` |
| `field.expression` (≠ column name) | `meta.dimension.sql` (`dataset.col` ↔ `${TABLE}.col`) |
| `metric.name` | Lightdash scopes metric names per model, Ossie per semantic model, so the Ossie name is Lightdash's own field id `<model>_<metric>` (`orders_total_amount`); the bare name is stashed in the extension and restored on export. An Ossie metric with no stash exports under its name minus a `<model>_` prefix, if it has one |
| `metric.datatype` | derived on import: `Integer` for counts, `Decimal` for numeric aggregates over a `number` column, the column's type for `min`/`max`, the declared type for `boolean`/`string`/`date`/`timestamp` metrics |
| `metric` that is one aggregation over a column (`SUM(ds.col)`, `COUNT(DISTINCT ds.col)`, `SUM(DISTINCT ds.col)`, `PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY ds.col)`, ...) | column-level `meta.metrics.<name>` with a typed metric (`sum`, `count_distinct`, `sum_distinct`, `percentile` + `percentile: 90`, ...) |
| `metric` that is one aggregation over any other expression (`AVG(CASE WHEN ds.status = 'done' THEN 1 ELSE 0 END)`) | model-level `meta.metrics.<name>` with the typed metric and the operand as `sql` |
| `metric` with any other expression | model-level `meta.metrics.<name>` with `type: number` + `sql`, on the dataset that joins every other dataset the expression references (`${joined_model.col}`) |
| `relationship` | `meta.joins` (`sql_on` built from / parsed into column pairs, `relationship: many-to-one` unless a stashed one says otherwise); the `alias` and other join attributes (`relationship`, `type`, `fields`, ...) travel in the relationship's `lightdash` extension, and a dataset joined more than once from the same model is aliased on export |
| `${TABLE}.col`, `${col}`, `${other_model.col}` in Lightdash SQL | `dataset.col` / `other_model.col`; `${alias.col}` is flattened onto the aliased model, `${metric}` is replaced by that metric's expression |
| Lightdash presentation attributes (`label`, `format`, `round`, `compact`, `group_label`, `hidden`, ...) | `custom_extensions` with `vendor_name: "lightdash"`; on export the extension data is overlaid onto the generated definition (structural keys — `sql`/`label` on dimensions, `sql`/`description` on metrics and `type`/`percentile` on metrics whose expression is a recognised aggregation, `join`/`sql_on` on joins — are protected and cannot be overridden) |

## Dialects

Lightdash SQL is written for the project's warehouse, so `import --dialect`
labels the emitted expressions with that warehouse's Ossie dialect
(`BIGQUERY`, `SNOWFLAKE`, `DATABRICKS`); warehouses without an Ossie dialect
(Postgres, Redshift, ...) keep the default `ANSI_SQL`. `export --dialect`
prefers that dialect, falls back to `ANSI_SQL`, and takes the first available
dialect with a `DIALECT_UNAVAILABLE` issue when an expression offers neither.

## Where the meta lives

dbt 1.10+ places `meta` under `config:`; Lightdash reads both the top-level
`meta` and `config.meta` and lets `config.meta` win, and so does the import
direction. Export writes top-level `meta` by default; pass
`--meta-under-config` to emit the `config.meta` placement instead.

## Recommended source shape for dbt-native flows

If the Ossie documents are also consumed by dbt's native OSI parsing, prefer
importing **without** `--database` (i.e. `schema.table` sources): the database
is usually environment-dependent in dbt projects, and a database-less source
keeps one document valid across environments (see
[dbt-core#15649](https://github.com/dbt-labs/dbt-core/issues/15649)).
Omitting `--schema` as well is reported as a `SOURCE_UNQUALIFIED` issue.

## Known limitations

- **A metric spanning datasets none of which joins all the others is
  dropped on export** (`CROSS_DATASET_METRIC_DROPPED`): Lightdash resolves
  `${other.column}` only through the joins the hosting model declares, never
  transitively. A field expression referencing an unjoined dataset is emitted
  as-is with a `FIELD_REFERENCE_UNJOINED` issue.
- **A dataset joined more than once** is referenced through its first join
  when an expression names it (`date_dim.year` → `${date_dim.year}` rather
  than the aliased second join).
- **Parameter and user-attribute references** (`${lightdash.parameters.x}`,
  `${ld.user.email}`) and **Liquid templating** (`{% if ld.query.filters … %}`)
  are evaluated by Lightdash at query time and have no Ossie form: a dimension
  or metric whose SQL uses them is skipped on import with an
  `EXPRESSION_NOT_PORTABLE` issue.
- **Metric names are normalised on the first round trip**: an Ossie metric
  named `total_sales` on `store_sales` comes back as `store_sales_total_sales`
  after Lightdash → Ossie, and stays stable from then on. A name that still
  collides after qualification (model `orders` + metric `x_total` vs model
  `orders_x` + metric `total`) is suffixed with a `METRIC_NAME_COLLISION`
  issue.
- **Metric-to-metric references** (`${other_metric}`) are inlined on import
  (`METRIC_REFERENCE_INLINED`), since Ossie metrics cannot reference each
  other; the export direction does not reconstruct the reference.
- **References through a join alias** (`${sold_date.year}`) are rewritten to
  the joined dataset (`date_dim.year`) with an `ALIAS_REFERENCE_FLATTENED`
  issue: Ossie has no aliases, so which of several joins to the same dataset
  was meant is not preserved in the expression.
- **`unique_keys` are not exported** — Lightdash has no corresponding
  concept — and consequently cannot be reconstructed on import.
- **A label or `ai_context` on a measure-only field is dropped on export**
  (`FIELD_ATTRIBUTE_NOT_REPRESENTABLE`): Lightdash keeps both on dimensions
  only, and writing them would turn the field into a dimension.
- **`dataset.name` is not preserved when it differs from the source table
  name**: the dbt model is named after the table part of `source`, and the
  import direction derives dataset names from model names. References inside
  expressions and relationships are rewritten consistently, but a
  name-stable round-trip is not guaranteed.
- **Relationships with mismatched `from_columns` / `to_columns` lengths are
  skipped on export** with a `RELATIONSHIP_COLUMNS_MISMATCHED` issue.
- **Datatypes round-trip by category, not by exact type**: Lightdash types are
  coarser than Ossie datatypes, so `Integer` comes back as `Decimal` and
  `DateTimeTz` as `DateTime`.
- **A measure-only field (no `dimension`) loses its `datatype`**: Lightdash
  carries types on dimensions only, so there is nowhere to put it.
- **A non-temporal time axis** (`is_time: true` on an `Integer` year) is
  reported with a `TIME_ROLE_NOT_REPRESENTABLE` issue on export.
- **Model-level Lightdash meta beyond `metrics` and `joins`** (`label`,
  `group_details`, `sql_filter`, `order_fields_by`, column
  `additional_dimensions`, ...) is not carried yet.
- **Standalone Lightdash YAML projects** (Lightdash without dbt) are not
  supported yet; the converter targets the dbt-meta flavour.
- Custom extensions from other vendors are ignored on export (reported as
  `FOREIGN_EXTENSION_IGNORED`); they remain untouched in the Ossie document.
- Documents are emitted at the current in-repo spec version. Note that
  dbt-core 1.12's native OSI parsing accepts spec versions `0.1.0` / `0.1.1`
  only.
