"""Static check (issue #322 acceptance criterion, carried forward by #404):

`app/routes/groups.py` and `frontend/src/pages/PatientsPage.js` must not import
anything from the Measure pipeline. The module is architecturally independent —
shared infra (fhir_client, dependencies, db) is allowed; job/measure/result
modules are not. The frontend half of this check lives in PatientsPage.test.js.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "app"
GROUPS_PY = BACKEND_ROOT / "routes" / "groups.py"

FORBIDDEN_MODULE_PREFIXES = (
    "app.orchestrator",
    "app.routes.jobs",
    "app.routes.measures",
    "app.routes.results",
    "app.routes.validation",
    "app.models.job",
    "app.models.validation",
    "app.services.orchestrator",
    "app.services.validation",
)


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_routes_groups_has_no_measure_pipeline_imports():
    source = GROUPS_PY.read_text()
    mods = _imported_modules(source)
    offenders = [m for m in mods if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)]
    assert not offenders, (
        f"app/routes/groups.py imports forbidden measure-pipeline modules: "
        f"{offenders}. The Groups feature must remain architecturally independent."
    )
