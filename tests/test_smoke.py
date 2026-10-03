import importlib

import pytest

SUBPACKAGES = ["core", "venues", "storage", "ingest", "models", "paper", "scoring"]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name):
    importlib.import_module(f"profit_engine.{name}")
