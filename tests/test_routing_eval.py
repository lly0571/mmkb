from dataclasses import asdict
import hashlib
import json
from unittest.mock import Mock, patch

import pytest

from mmkb.image_types import IMAGE_CATEGORIES, ROUTING_POLICY_VERSION
from mmkb.routing import ClassificationResult
from mmkb.routing_eval import evaluate, load_manifest
from mmkb.routing_eval import capture
from mmkb.config import ModelConfig, ServiceConfig
from mmkb.providers import Providers
from mmkb.providers.systemone import Answer


def frozen_run(tmp_path):
    samples, rows = [], {}
    for label in IMAGE_CATEGORIES:
        for split in ("calibration", "validation"):
            sid = f"{label}-{split}"
            asset = tmp_path / f"{sid}.png"
            asset.write_bytes(sid.encode())
            samples.append({"sample_id": sid, "expected": label, "split": split,
                            "file": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()})
            p = {name: .94 if name == label else .02 for name in IMAGE_CATEGORIES}
            rows[sid] = {
                "systemone": {"result": asdict(ClassificationResult(label, "systemone", p, .9, .95, True)), "elapsed_ms": 10},
                "llm": {"result": asdict(ClassificationResult(label, "llm")), "elapsed_ms": 20},
            }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "policy_version": ROUTING_POLICY_VERSION, "samples": samples}))
    _, fingerprint = load_manifest(manifest)
    capture = tmp_path / "capture.json"
    data = {"policy_version": ROUTING_POLICY_VERSION, "manifest_sha256": fingerprint,
            "models": {"systemone": "test", "llm": "test"}, "samples": rows}
    capture.write_text(json.dumps(data))
    return manifest, capture, data


def test_calibration_and_holdout_are_separate_and_ties_prefer_stricter_thresholds(tmp_path):
    manifest, capture, data = frozen_run(tmp_path)
    good = evaluate(manifest, capture)
    assert good["passed"] is True and good["validation_correct"] == 4
    assert good["selected_policy"]["min_confidence"] == .8
    # Holdout errors cannot influence threshold selection, and must fail acceptance.
    entry = data["samples"]["visdoc-validation"]["systemone"]["result"]
    entry["workflow"] = "visual_image"
    entry["probabilities"] = {name: .94 if name == "visual_image" else .02 for name in IMAGE_CATEGORIES}
    capture.write_text(json.dumps(data))
    failed = evaluate(manifest, capture)
    assert failed["selected_policy"] == good["selected_policy"]
    assert failed["passed"] is False


def test_missing_service_cannot_be_reported_as_online_acceptance(tmp_path):
    manifest, capture, data = frozen_run(tmp_path)
    for row in data["samples"].values():
        row["systemone"] = {"error": "unavailable", "elapsed_ms": 0}
    capture.write_text(json.dumps(data))
    result = evaluate(manifest, capture)
    assert result["validation_correct"] == 4 and result["services_validated"] is False
    assert result["passed"] is False


def test_frozen_checksums_and_capture_fingerprint_are_enforced(tmp_path):
    manifest, capture, data = frozen_run(tmp_path)
    data["manifest_sha256"] = "different"
    capture.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="differ"):
        evaluate(manifest, capture)
    asset = tmp_path / "visdoc-calibration.png"
    asset.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        load_manifest(manifest)


def test_declared_caption_fallback_is_reported_as_degraded_not_full_enhancement(tmp_path):
    manifest, path, data = frozen_run(tmp_path)
    data["process"] = True
    for sid, row in data["samples"].items():
        label = sid.rsplit("-", 1)[0]
        augmented = label in {"structured_image", "info_rich_image"}
        row["processing"] = {"workflow": label, "embedding_dimensions": 2048, "embedding_norm": 1,
                             "encoding": "image_only", "caption_chars": 0, "ocr_text": None,
                             "key_text_hits": {}, "warnings": [{"stage": "caption", "code": "output_truncated"}] if augmented else []}
    path.write_text(json.dumps(data))
    report = evaluate(manifest, path)
    assert report["passed"] is True and report["degraded"] is True
    data["samples"]["structured_image-validation"]["processing"]["warnings"] = []
    path.write_text(json.dumps(data))
    assert evaluate(manifest, path)["processing_passed"] is False


def test_reusing_identical_classifications_never_reissues_model_calls(tmp_path):
    manifest, _, _ = frozen_run(tmp_path)
    p = Providers(Mock(), Mock(), Mock())

    def choose(*args, images, **kwargs):
        label = images[0].data.decode().rsplit("-", 1)[0]
        probabilities = {name: .94 if name == label else .02 for name in IMAGE_CATEGORIES}
        return Answer("image_type", "choice", probabilities, .9, choice=label, label_mass=.95)

    p.decision.choose.side_effect = choose
    p.vision.classify.side_effect = lambda image: image.data.decode().rsplit("-", 1)[0]
    service = ServiceConfig("https://model.example/v1", "test")
    with patch("mmkb.routing_eval.load_model_config", return_value=ModelConfig(service, service, service)), \
            patch("mmkb.routing_eval.build_providers", return_value=p):
        first = tmp_path / "first.json"
        capture(manifest, first)
        assert p.decision.choose.call_count == p.vision.classify.call_count == 8
        p.decision.choose.reset_mock()
        p.vision.classify.reset_mock()
        result = capture(manifest, tmp_path / "reused.json", reuse_classification=first)
        p.decision.choose.assert_not_called()
        p.vision.classify.assert_not_called()
        assert result["classification_source_sha256"]
        changed = json.loads(first.read_text())
        changed["classification_sha256"] = "different"
        first.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="reuse"):
            capture(manifest, tmp_path / "invalid.json", reuse_classification=first)
