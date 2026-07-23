# conftest.py — shared pytest fixtures and import-path setup for SentinelOps tests.
#
# Registers the hyphenated `incident-normalizer` directory as an importable
# Python module under the snake_case alias `incident_normalizer` so that test
# files can do `from incident_normalizer import webhook_server` without hitting
# the Python rule that hyphens are not valid in identifiers.
#
# Also ensures the repo root is on sys.path for `agents`, `memory`, etc.

import importlib
import importlib.machinery
import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_HYPHEN_DIR = os.path.join(_REPO_ROOT, "incident-normalizer")
_ALIAS = "incident_normalizer"


# ── Register incident-normalizer as importable `incident_normalizer` ─────────

def _register_normalizer_alias() -> None:
    """Map `incident_normalizer` -> the `incident-normalizer/` directory."""
    if _ALIAS in sys.modules:
        return
    init_path = os.path.join(_HYPHEN_DIR, "__init__.py")
    loader = importlib.machinery.SourceFileLoader(_ALIAS, init_path)
    spec = importlib.util.spec_from_file_location(
        _ALIAS,
        location=init_path,
        submodule_search_locations=[_HYPHEN_DIR],
        loader=loader,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ALIAS] = module
    loader.exec_module(module)


_register_normalizer_alias()


import warnings  # noqa: E402

# Pydantic v2 .dict() is deprecated; suppress the deprecation warning in tests
# so they pass cleanly while we migrate to .model_dump() incrementally.
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydantic.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="agents.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="incident_normalizer.*")
# fastapi's testclient warns about httpx usage on newer starlette; non-fatal.
warnings.filterwarnings("ignore", category=DeprecationWarning, module="starlette.*")
