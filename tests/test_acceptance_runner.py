"""Regression checks for the acceptance runner's optional vision contract."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("warnings,expected", [
    ([{"stage": "caption", "code": "output_truncated"}], True),
    ([], False),
    ([{"stage": "ocr", "code": "output_truncated"}], False),
])
def test_initial_caption_may_be_missing_only_with_its_truncation_warning(warnings, expected):
    path = Path(__file__).resolve().parents[1] / "scripts/acceptance/run_backend_acceptance.py"
    spec = importlib.util.spec_from_file_location("acceptance_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = object.__new__(module.Acceptance)
    runner.samples = {"chart": {"route": "structured_image"}}
    runner.case = Mock()
    runner.end = lambda status, detail: status == module.PASS
    runner.wait_terminal = lambda sid: (True, {
        "status": "ready", "workflow": "structured_image", "caption": None,
        "extra": {"processing": {"warnings": warnings}},
    }, "")
    assert runner.live02() is expected
