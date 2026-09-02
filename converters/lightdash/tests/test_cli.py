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
    assert main(["export", str(TPCDS_PATH), str(schema_yml)]) == 0

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
