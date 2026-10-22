"""Contract tests: fixtures, contract models and synthetic adapters stay in lockstep."""

import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from alsense_chat.adapters.contracts import ErrorResponse
from alsense_chat.adapters.errors import ERRORS, ModelUnavailable

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_script", ROOT / "scripts" / "fixtures.py")
assert spec and spec.loader
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
CASES = fixtures.cases()
GENERATED = fixtures.generate()


@pytest.mark.parametrize("relative", sorted(GENERATED))
def test_committed_fixture_matches_the_synthetic_adapters(relative):
    path = ROOT / "fixtures" / relative
    assert path.is_file(), f"Missing fixture. Run: uv run python scripts/dev.py fixtures ({relative})"
    assert path.read_text(encoding="utf-8") == GENERATED[relative], (
        f"fixtures/{relative} drifted. Run: uv run python scripts/dev.py fixtures"
    )


@pytest.mark.parametrize("relative", sorted(CASES))
def test_fixture_validates_against_its_contract(relative):
    contract, _ = CASES[relative]
    data = json.loads((ROOT / "fixtures" / relative).read_text(encoding="utf-8"))
    TypeAdapter(contract).validate_python(data)


def test_every_fixture_file_has_a_producing_case():
    committed = {
        str(path.relative_to(ROOT / "fixtures")) for path in (ROOT / "fixtures").rglob("*.json") if path.is_file()
    }
    assert committed == set(GENERATED)


def test_error_fixtures_use_the_documented_status_for_their_category():
    for relative, (contract, _) in CASES.items():
        if contract is not ErrorResponse:
            continue
        error = ErrorResponse.model_validate_json((ROOT / "fixtures" / relative).read_text())
        assert ERRORS[error.category].http_status == error.http_status, relative
    assert ModelUnavailable().http_status == 503


def test_fixture_readme_lists_every_fixture():
    readme = (ROOT / "fixtures" / "README.md").read_text()
    for relative in CASES:
        assert f"`{relative}`" in readme, relative
