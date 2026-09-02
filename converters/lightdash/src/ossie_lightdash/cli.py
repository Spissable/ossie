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

"""Command line interface for the Ossie <> Lightdash converter."""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import yaml

from ossie import OssieDialect, OssieDocument
from ossie_lightdash.catalog import load_catalog
from ossie_lightdash.converter_issues import ISSUE_EXPLANATIONS
from ossie_lightdash.dbt_project import load_schema
from ossie_lightdash.lightdash_to_ossie import LightdashToOssieConverter
from ossie_lightdash.ossie_to_lightdash import OssieToLightdashConverter


def _read_document(path: Path) -> OssieDocument:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return OssieDocument.model_validate_json(text)
    return OssieDocument.model_validate(yaml.safe_load(text))


# Ossie dialects that name a Lightdash warehouse type.
_WAREHOUSE_BY_DIALECT = {
    OssieDialect.BIGQUERY: "bigquery",
    OssieDialect.SNOWFLAKE: "snowflake",
    OssieDialect.DATABRICKS: "databricks",
}


def _write_lightdash_project(
    models, output: Path, *, name: str, warehouse: Optional[str]
) -> None:
    """Write one model file per dataset plus a starter lightdash.config.yml.

    Files go to ``<output>/lightdash/models/<model>.yml``, the layout
    ``lightdash deploy`` looks for; an existing config is left alone.
    """
    models_dir = output / "lightdash" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for model in models:
        (models_dir / f"{model['name']}.yml").write_text(
            yaml.safe_dump(model, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    config = output / "lightdash.config.yml"
    if not config.exists():
        config.write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "version": "1.0",
                    "warehouse": {"type": warehouse or "CHANGE_ME"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        if warehouse is None:
            print(
                "lightdash.config.yml: set warehouse.type (pass --warehouse, or a "
                "--dialect Lightdash knows: BIGQUERY, SNOWFLAKE, DATABRICKS)",
                file=sys.stderr,
            )


def _print_issues(issues) -> None:
    """One line per issue, with the explanation on its first occurrence."""
    explained = set()
    for issue in issues:
        line = f"[{issue.issue_type.value}] {issue.element_name}"
        if issue.issue_type not in explained:
            explained.add(issue.issue_type)
            line += f"  -- {ISSUE_EXPLANATIONS.get(issue.issue_type, '')}".rstrip(" -")
        print(line, file=sys.stderr)
    if issues:
        print(f"{len(issues)} issue(s); everything else converted cleanly.", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ossie-lightdash")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser(
        "export",
        help="Ossie document (.json/.yaml) -> Lightdash model files, or a dbt schema.yml",
    )
    export_parser.add_argument("input", type=Path)
    export_parser.add_argument(
        "output",
        type=Path,
        help="project directory (lightdash-yml) or schema file (dbt-meta)",
    )
    export_parser.add_argument(
        "--format",
        choices=["lightdash-yml", "dbt-meta"],
        default="lightdash-yml",
        help="lightdash-yml: Lightdash's dbt-free model files, deployable as they are "
        "(default); dbt-meta: one dbt schema.yml with Lightdash meta blocks",
    )
    export_parser.add_argument(
        "--warehouse",
        default=None,
        help="warehouse.type for the generated lightdash.config.yml "
        "(default: derived from --dialect when possible)",
    )
    export_parser.add_argument(
        "--dialect",
        choices=[dialect.name for dialect in OssieDialect],
        default=OssieDialect.ANSI_SQL.name,
        help="preferred expression dialect (falls back to ANSI_SQL)",
    )
    export_parser.add_argument(
        "--meta-under-config",
        action="store_true",
        help="write Lightdash meta under `config:` (dbt 1.10+) instead of top-level `meta:`",
    )

    import_parser = subparsers.add_parser(
        "import",
        help="Lightdash dbt schema.yml, or a dbt project directory -> Ossie document (.json/.yaml)",
    )
    import_parser.add_argument(
        "input", type=Path, help="a schema file, or a directory walked for models: and seeds:"
    )
    import_parser.add_argument("output", type=Path)
    import_parser.add_argument("--database", default=None)
    import_parser.add_argument("--schema", default=None)
    import_parser.add_argument(
        "--semantic-model-name", default="lightdash_semantic_model"
    )
    import_parser.add_argument(
        "--dialect",
        choices=[dialect.name for dialect in OssieDialect],
        default=OssieDialect.ANSI_SQL.name,
        help="dialect the Lightdash SQL is written in (the project's warehouse)",
    )
    import_parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="dbt target/catalog.json (from `dbt docs generate`): warehouse column "
        "types fill in datatypes for columns without an authored type",
    )

    args = parser.parse_args(argv)

    if args.command == "export":
        dialect = OssieDialect[args.dialect]
        document = _read_document(args.input)
        converter = OssieToLightdashConverter(dialect, meta_under_config=args.meta_under_config)
        if args.format == "lightdash-yml":
            result = converter.convert_models(document)
            _write_lightdash_project(
                result.output,
                args.output,
                name=document.semantic_model[0].name if document.semantic_model else "ossie",
                warehouse=args.warehouse or _WAREHOUSE_BY_DIALECT.get(dialect),
            )
            summary = (
                f"Wrote {len(result.output)} model file(s) to {args.output / 'lightdash' / 'models'}"
                f" and {args.output / 'lightdash.config.yml'}; run `lightdash compile` there."
            )
        else:
            result = converter.convert(document)
            args.output.write_text(
                yaml.safe_dump(result.output, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            summary = f"Wrote {len(result.output['models'])} model(s) to {args.output}."
    else:
        schema_yml = load_schema(args.input)
        result = LightdashToOssieConverter(OssieDialect[args.dialect]).convert(
            schema_yml,
            database=args.database,
            schema=args.schema,
            semantic_model_name=args.semantic_model_name,
            catalog=load_catalog(args.catalog) if args.catalog else None,
        )
        semantic_model = result.output.semantic_model[0]
        summary = (
            f"Wrote {len(semantic_model.datasets or [])} dataset(s), "
            f"{len(semantic_model.metrics or [])} metric(s), "
            f"{len(semantic_model.relationships or [])} relationship(s) to {args.output}."
        )
        document = result.output.model_dump(mode="json", by_alias=True, exclude_none=True)
        if args.output.suffix == ".json":
            args.output.write_text(
                json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        else:
            args.output.write_text(
                yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    # Issues first, then what was written, all on stderr like the other converters.
    _print_issues(result.issues)
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
