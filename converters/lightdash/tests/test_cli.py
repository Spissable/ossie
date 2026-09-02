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
"""Command line round trips through both output formats."""

from pathlib import Path

import pytest
import yaml

from ossie import OssieDocument
from ossie_lightdash.cli import main

TPCDS_PATH = Path(__file__).parent / ".." / ".." / ".." / "examples" / "tpcds_semantic_model.yaml"


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_import_writes_a_loadable_document(tmp_path, suffix):
    schema_yml = tmp_path / "schema.yml"
    assert main(["export", str(TPCDS_PATH), str(schema_yml), "--format", "dbt-meta"]) == 0

    document_path = tmp_path / f"semantic_model{suffix}"
    assert main(["import", str(schema_yml), str(document_path), "--schema", "public"]) == 0

    text = document_path.read_text(encoding="utf-8")
    if suffix == ".json":
        document = OssieDocument.model_validate_json(text)
    else:
        document = OssieDocument.model_validate(yaml.safe_load(text))
    assert {dataset.name for dataset in document.semantic_model[0].datasets} == {
        "store_sales", "date_dim", "customer", "item", "store"
    }

def test_export_writes_a_lightdash_project(tmp_path, capsys):
    project = tmp_path / "project"
    assert main(["export", str(TPCDS_PATH), str(project), "--dialect", "BIGQUERY"]) == 0
    captured = capsys.readouterr()
    # Like the other converters: stdout stays clean, stderr carries the report.
    assert captured.out == ""
    assert captured.err.splitlines()[-1].startswith("Wrote 5 model file(s) to ")
    # The one loss is named, explained once, and counted.
    assert captured.err.splitlines()[:2] == [
        "[TIME_ROLE_NOT_REPRESENTABLE] d_year  -- is_time on a non-date type (e.g. an integer year); "
        "Lightdash has no such marker, the column is a plain dimension",
        "1 issue(s); everything else converted cleanly.",
    ]
    files = sorted(p.name for p in (project / "lightdash" / "models").iterdir())
    assert files == ["customer.yml", "date_dim.yml", "item.yml", "store.yml", "store_sales.yml"]
    model = yaml.safe_load((project / "lightdash" / "models" / "store_sales.yml").read_text())
    assert model["type"] == "model"
    assert model["sql_from"] == "tpcds.public.store_sales"
    assert all({"name", "type", "sql"} <= set(d) for d in model["dimensions"])
    config = yaml.safe_load((project / "lightdash.config.yml").read_text())
    assert config["warehouse"] == {"type": "bigquery"}
    assert config["name"] == "tpcds_retail_model"
    # A second export leaves an existing config alone.
    (project / "lightdash.config.yml").write_text("name: mine\nversion: '1.0'\nwarehouse:\n  type: postgres\n")
    assert main(["export", str(TPCDS_PATH), str(project)]) == 0
    assert yaml.safe_load((project / "lightdash.config.yml").read_text())["name"] == "mine"


def test_export_dbt_meta_still_writes_one_schema_file(tmp_path):
    schema_yml = tmp_path / "schema.yml"
    assert main(["export", str(TPCDS_PATH), str(schema_yml), "--format", "dbt-meta"]) == 0
    assert yaml.safe_load(schema_yml.read_text())["version"] == 2

def test_import_reads_a_whole_dbt_project(tmp_path):
    project = tmp_path / "dbt"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "target").mkdir()
    (project / "dbt_project.yml").write_text("name: p\nmodels:\n  p:\n    +materialized: table\n")
    (project / "models" / "orders.yml").write_text(
        "models:\n  - name: orders\n    columns:\n      - name: amount\n        meta:\n          metrics:\n            total: {type: sum}\n"
    )
    (project / "models" / "marts" / "customers.yaml").write_text(
        "models:\n  - name: customers\n    columns:\n      - name: id\n"
    )
    (project / "data.yml").write_text("seeds:\n  - name: statuses\n    columns:\n      - name: code\n")
    (project / "target" / "stale.yml").write_text("models:\n  - name: stale\n")

    document_path = tmp_path / "model.yaml"
    assert main(["import", str(project), str(document_path), "--schema", "marts"]) == 0
    document = OssieDocument.model_validate(yaml.safe_load(document_path.read_text()))
    assert [d.name for d in document.semantic_model[0].datasets] == ["customers", "orders", "statuses"]
    assert document.semantic_model[0].metrics[0].name == "orders_total"
