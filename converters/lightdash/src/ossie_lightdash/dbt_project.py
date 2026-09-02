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
from typing import Any, Dict, List

import yaml

# Directories dbt generates; nothing in them is authored schema.
_SKIPPED_DIRS = {"target", "dbt_packages", "logs", ".git", "node_modules"}


def load_schema(path: Path) -> Dict[str, Any]:
    """Return ``{"version": 2, "models": [...], "seeds": [...]}`` for ``path``.

    A file is read as is. A directory is walked in sorted order; every
    ``.yml`` / ``.yaml`` file contributes its list-valued ``models:`` and
    ``seeds:`` entries (``dbt_project.yml`` has a dict-valued ``models:`` and
    is skipped by that rule), and generated directories are ignored.
    """
    if path.is_file():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    models: List[Dict[str, Any]] = []
    seeds: List[Dict[str, Any]] = []
    for file in sorted(path.rglob("*.y*ml")):
        if file.suffix not in (".yml", ".yaml"):
            continue
        if _SKIPPED_DIRS & set(file.relative_to(path).parts[:-1]):
            continue
        document = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(document, dict):
            continue
        if isinstance(document.get("models"), list):
            models.extend(document["models"])
        if isinstance(document.get("seeds"), list):
            seeds.extend(document["seeds"])
    return {"version": 2, "models": models, "seeds": seeds}
