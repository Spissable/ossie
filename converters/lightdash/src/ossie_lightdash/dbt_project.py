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
"""Read Lightdash-flavoured dbt schema definitions from a file or a project.

dbt lets a project spread its ``models:`` and ``seeds:`` entries over any
number of YAML files, so ``import`` accepts a directory as well as a single
schema file and merges everything it finds.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

# Directories dbt or Python tooling generate; nothing in them is authored schema.
_SKIPPED_DIRS = {"target", "dbt_packages", "logs", ".git", "node_modules", "env", "venv", ".venv", "site-packages", "__pycache__"}


def load_schema(path: Path) -> Dict[str, Any]:
    """Return ``{"version": 2, "models": [...], "seeds": [...]}`` for ``path``."""
    schema, _ = load_schema_with_skips(path)
    return schema


def load_schema_with_skips(path: Path) -> Tuple[Dict[str, Any], List[Path]]:
    """``load_schema`` plus the files that were skipped because they are not
    valid YAML (templates with placeholders, Jinja-only files, ...).

    A file is read as is. A directory is walked in sorted order; every
    ``.yml`` / ``.yaml`` file contributes its list-valued ``models:`` and
    ``seeds:`` entries (``dbt_project.yml`` has a dict-valued ``models:`` and
    is skipped by that rule), and generated directories are ignored.
    """
    if path.is_file():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}, []

    models: List[Dict[str, Any]] = []
    seeds: List[Dict[str, Any]] = []
    skipped: List[Path] = []
    for file in sorted(path.rglob("*.y*ml")):
        if file.suffix not in (".yml", ".yaml"):
            continue
        if _SKIPPED_DIRS & set(file.relative_to(path).parts[:-1]):
            continue
        try:
            document = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            skipped.append(file)
            continue
        if not isinstance(document, dict):
            continue
        if isinstance(document.get("models"), list):
            models.extend(document["models"])
        if isinstance(document.get("seeds"), list):
            seeds.extend(document["seeds"])
    return {"version": 2, "models": models, "seeds": seeds}, skipped
