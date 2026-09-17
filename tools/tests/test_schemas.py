"""Schema and fixture validation tests (no device required)."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, ValidationError, validate

TOOLS_DIR = Path(__file__).resolve().parents[1]
SCHEMA_DIR = TOOLS_DIR / "schemas"
FIXTURE_DIR = SCHEMA_DIR / "fixtures"

SCHEMA_NAMES = ["perf_frame", "perf_sample", "coverage", "audit",
                "chaos_snapshot", "crash_cluster", "root_cause_pack"]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft07(name):
    schema = _load(SCHEMA_DIR / f"{name}.schema.json")
    Draft7Validator.check_schema(schema)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_valid_fixture_passes(name):
    schema = _load(SCHEMA_DIR / f"{name}.schema.json")
    instance = _load(FIXTURE_DIR / f"valid_{name}.json")
    validate(instance=instance, schema=schema)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_invalid_fixture_raises(name):
    schema = _load(SCHEMA_DIR / f"{name}.schema.json")
    instance = _load(FIXTURE_DIR / f"invalid_{name}.json")
    with pytest.raises(ValidationError):
        validate(instance=instance, schema=schema)
