"""Capture both image classifiers once, then calibrate and evaluate without more model calls."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from itertools import product
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from mmkb.config import RoutingConfig, load_model_config
from mmkb.image_types import IMAGE_CATEGORIES, IMAGE_WORKFLOWS, ROUTING_POLICY_VERSION
from mmkb.processing import Processor, ResourceSnapshot
from mmkb.providers import ImageInput, ProviderError, build_providers
from mmkb.routing import ClassificationResult, LLMClassifier, SystemOneClassifier, decision_rejection
from mmkb.providers.vision import CLASSIFY_PROMPT, INFO_RICH_PROMPT, STRUCTURED_PROMPT


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def load_manifest(path: Path):
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1 or data.get("policy_version") != ROUTING_POLICY_VERSION:
        raise ValueError("Unsupported sample manifest")
    samples = data["samples"]
    ids = set()
    for sample in samples:
        sid = sample["sample_id"]
        if sid in ids or sample["expected"] not in IMAGE_WORKFLOWS or sample["split"] not in {"calibration", "validation"}:
            raise ValueError("Invalid sample label, split or duplicate ID")
        ids.add(sid)
        asset = path.parent / sample["file"]
        if asset.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Sample exceeds upload size limit")
        if hashlib.sha256(asset.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError(f"Sample checksum changed: {sid}")
    for split in ("calibration", "validation"):
        if {s["expected"] for s in samples if s["split"] == split} != IMAGE_WORKFLOWS:
            raise ValueError("Every split must cover all four classes")
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data, fingerprint


def capture(manifest_path: Path, output: Path, *, process: bool = False, reuse_classification: Path | None = None):
    manifest, fingerprint = load_manifest(manifest_path)
    config = load_model_config()
    models = {"embedding": config.embedding.model, "llm": config.caption.model if config.caption else None,
              "systemone": config.decision.model if config.decision else None}
    data = {"schema_version": 1, "policy_version": ROUTING_POLICY_VERSION, "manifest_sha256": fingerprint,
            "models": models, "process": process,
            "classification_sha256": hashlib.sha256((CLASSIFY_PROMPT + json.dumps(IMAGE_CATEGORIES, ensure_ascii=False)).encode()).hexdigest(),
            "encoding_sha256": hashlib.sha256((INFO_RICH_PROMPT + STRUCTURED_PROMPT).encode()).hexdigest(),
            "classification_source_sha256": None,
            "captured_at": datetime.now(timezone.utc).isoformat(), "samples": {}}
    reused = None
    if reuse_classification is not None:
        reused = json.loads(reuse_classification.read_text())
        if any(reused.get(key) != data[key] for key in (
            "manifest_sha256", "models", "classification_sha256", "policy_version",
        )) or set(reused["samples"]) != {s["sample_id"] for s in manifest["samples"]}:
            raise ValueError("Cannot reuse different or incomplete classifications")
        data["classification_source_sha256"] = hashlib.sha256(reuse_classification.read_bytes()).hexdigest()
    if output.exists():
        previous = json.loads(output.read_text())
        if any(previous.get(key) != data[key] for key in (
            "manifest_sha256", "models", "process", "classification_sha256", "encoding_sha256", "classification_source_sha256",
        )):
            raise ValueError("Capture differs from this manifest, model configuration or processing mode")
        data = previous
    with build_providers(config) as providers:
        for sample in manifest["samples"]:
            sid = sample["sample_id"]
            if sid in data["samples"]:
                continue
            path = manifest_path.parent / sample["file"]
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]
            image = ImageInput(path.read_bytes(), mime)
            row = {}
            for backend, provider, adapter in (("systemone", providers.decision, SystemOneClassifier),
                                                ("llm", providers.vision, LLMClassifier)):
                if reused is not None:
                    row[backend] = reused["samples"][sid][backend]
                    continue
                started = time.monotonic()
                try:
                    row[backend] = {"result": asdict(adapter(provider).classify(image))} if provider else {"error": "unavailable"}
                except ProviderError as exc:
                    row[backend] = {"error": exc.code}
                row[backend]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            if process:
                started = time.monotonic()
                try:
                    result = Processor(providers).process(
                        ResourceSnapshot(uuid4(), 1, "image", sample["expected"]), image=image,
                    )
                    row["processing"] = {
                        "workflow": result.workflow, "encoding": result.encoding, "warnings": result.warnings,
                        "embedding_dimensions": len(result.embedding), "embedding_norm": math.hypot(*result.embedding),
                        "caption_chars": len(result.caption or ""), "caption_preview": (result.caption or "")[:160],
                        "ocr_text": result.ocr_text,
                        "key_text_hits": {key: key in (result.caption or "") for key in sample.get("key_text", [])},
                    }
                except ProviderError as exc:
                    row["processing"] = {"error": exc.code}
                row["processing"]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            data["samples"][sid] = row
            write_json(output, data)
            print(f"captured {sid}", flush=True)
    return data


def replay(row, config: RoutingConfig):
    primary = row["systemone"]
    result = ClassificationResult(**primary["result"]) if "result" in primary else None
    code = decision_rejection(result, config) if result else primary.get("error", "unavailable")
    elapsed = primary["elapsed_ms"]
    calls = int(primary.get("error") != "unavailable")
    if code is None:
        return {"workflow": result.workflow, "source": "systemone", "calls": calls, "elapsed_ms": elapsed}
    llm = row["llm"]
    label = llm.get("result", {}).get("workflow")
    return {"workflow": label if label in IMAGE_WORKFLOWS else "visual_image",
            "source": "llm" if label in IMAGE_WORKFLOWS else "default", "decision_rejection": code,
            "calls": calls + int(llm.get("error") != "unavailable"), "elapsed_ms": elapsed + llm["elapsed_ms"]}


def evaluate(manifest_path: Path, capture_path: Path):
    manifest, fingerprint = load_manifest(manifest_path)
    captured = json.loads(capture_path.read_text())
    if captured.get("manifest_sha256") != fingerprint or captured.get("policy_version") != ROUTING_POLICY_VERSION:
        raise ValueError("Capture and frozen manifest differ")
    samples = manifest["samples"]
    rows = captured["samples"]
    if set(rows) != {s["sample_id"] for s in samples}:
        raise ValueError("Capture is incomplete")
    ranked = []
    for probability, confidence, mass in product((.6, .7, .8), (.4, .6, .8), (.5, .8, .9)):
        policy = RoutingConfig(min_probability=probability, min_confidence=confidence, min_label_mass=mass)
        accepted, wrong = 0, 0
        for sample in samples:
            if sample["split"] != "calibration":
                continue
            route = replay(rows[sample["sample_id"]], policy)
            if route["source"] == "systemone":
                accepted += 1
                wrong += int(route["workflow"] != sample["expected"])
        if wrong == 0:
            ranked.append(((accepted, confidence, mass, probability), policy))
    if not ranked:
        return {"passed": False, "reason": "no_zero_error_calibration_policy"}
    selected = max(ranked, key=lambda item: item[0])[1]
    results = []
    for sample in samples:
        row = rows[sample["sample_id"]]
        route = replay(row, selected)
        llm = row["llm"].get("result", {}).get("workflow")
        results.append({"sample_id": sample["sample_id"], "expected": sample["expected"], "split": sample["split"],
                        **route, "correct": route["workflow"] == sample["expected"],
                        "llm_workflow": llm, "llm_correct": llm == sample["expected"],
                        "llm_elapsed_ms": row["llm"]["elapsed_ms"]})
    validation = [r for r in results if r["split"] == "validation"]
    direct_classes = {r["expected"] for r in results if r["correct"] and r["source"] == "systemone"}
    ready = all("result" in row[backend] for row in rows.values() for backend in ("systemone", "llm"))
    processing_ok, warnings = None, {}
    if captured.get("process"):
        checks = []
        for sample in samples:
            result = rows[sample["sample_id"]]["processing"]
            if "error" in result:
                checks.append(False)
                continue
            augmented = sample["expected"] in {"structured_image", "info_rich_image"}
            has_caption = result["caption_chars"] > 0
            caption_failed = any(w["stage"] == "caption" for w in result["warnings"])
            expected_encoding = "image_caption" if augmented and has_caption else "image_only"
            checks.append(result["embedding_dimensions"] == 2048
                          and abs(result["embedding_norm"] - 1) < 1e-6
                          and result["workflow"] == sample["expected"]
                          and result["encoding"] == expected_encoding
                          and result["ocr_text"] is None
                          and (has_caption or caption_failed if augmented else not has_caption)
                          and all(result["key_text_hits"].values()))
            if result["warnings"]:
                warnings[sample["sample_id"]] = result["warnings"]
        processing_ok = all(checks)
    return {"schema_version": 1, "policy_version": ROUTING_POLICY_VERSION, "manifest_sha256": fingerprint,
            "models": captured["models"], "selected_policy": asdict(selected),
            "classification_sha256": captured.get("classification_sha256"),
            "encoding_sha256": captured.get("encoding_sha256"),
            "passed": ready and all(r["correct"] for r in validation) and direct_classes == IMAGE_WORKFLOWS
                      and processing_ok is not False,
            "processing_passed": processing_ok, "services_validated": ready,
            "degraded": bool(warnings), "processing_warnings": warnings,
            "validation_correct": sum(r["correct"] for r in validation), "validation_total": len(validation),
            "llm_validation_correct": sum(r["llm_correct"] for r in validation),
            "direct_classes": sorted(direct_classes), "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "evaluate"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--process", action="store_true", help="Also check encoding on the explicitly labelled routes")
    parser.add_argument("--reuse-classification", type=Path, help="Reuse identical frozen classification requests when only testing encoding changes")
    args = parser.parse_args()
    try:
        if args.command == "capture":
            capture(args.manifest, args.output, process=args.process, reuse_classification=args.reuse_classification)
        else:
            if args.capture is None:
                parser.error("evaluate requires --capture")
            result = evaluate(args.manifest, args.capture)
            write_json(args.output, result)
            print(json.dumps({key: value for key, value in result.items() if key != "results"}, ensure_ascii=False))
            if not result["passed"]:
                parser.exit(1)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"routing_evaluation_failed: {type(exc).__name__}; check configuration and frozen inputs\n")


if __name__ == "__main__":
    main()
