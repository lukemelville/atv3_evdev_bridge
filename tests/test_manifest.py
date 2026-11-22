from pathlib import Path

import yaml

import run


def test_manifest_defaults_pass_runtime_validation():
    manifest = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))

    config = run.load_config(manifest["options"])

    assert config.target_contains == run.DEFAULT_TARGET_CONTAINS
    assert config.event_type == run.DEFAULT_EVENT_TYPE
    assert config.hold_delay == run.DEFAULT_HOLD_DELAY
    assert config.hold_repeat == run.DEFAULT_HOLD_REPEAT


def test_manifest_uses_supervisor_compatible_string_schema():
    manifest = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))

    assert manifest["schema"]["target_contains"] == "str"
