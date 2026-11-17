"""Freeze the existing nine images plus three full pages of the public ColPali v6 PDF."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


PUBLIC_PDF = "https://arxiv.org/pdf/2407.01449v6"
PUBLIC_PDF_SHA256 = "852ec724f3d48316265611579f4c556a14fea24f29360b5929d02e4604662631"
CALIBRATION = {"s-cpu-spec", "v-skater", "d-vscode", "doc-text"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(existing_manifest: Path, pdf: Path, output: Path):
    if sha256(pdf) != PUBLIC_PDF_SHA256:
        raise ValueError("PDF does not match the reviewed public version")
    with tempfile.TemporaryDirectory(prefix="mmkb-routing-samples-") as directory:
        return _prepare(existing_manifest, pdf, output, Path(directory))


def _prepare(existing_manifest: Path, pdf: Path, output: Path, staging: Path):
    manifest = json.loads(existing_manifest.read_text())
    files = staging / "files"
    files.mkdir(parents=True, exist_ok=True)
    samples = []
    for sid, old in manifest["samples"].items():
        if old["route"] not in {"structured_image", "visual_image", "visdoc"}:
            continue
        source = existing_manifest.parent / "files" / old["file"]
        if sha256(source) != old["sha256"]:
            raise ValueError(f"Original sample checksum differs: {sid}")
        target = files / old["file"]
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        samples.append({"sample_id": sid, "file": f"files/{target.name}", "sha256": sha256(target),
                        "expected": "info_rich_image" if old["route"] == "visdoc" else old["route"],
                        "split": "calibration" if sid in CALIBRATION else "validation",
                        "key_text": old.get("ocr_snippets", []) if sid == "d-vscode" else []})
    pdf_sha = sha256(pdf)
    for sid, page in (("doc-text", 3), ("doc-table", 4), ("doc-chart", 2)):
        target = files / f"{sid}.png"
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1600",
                        "-png", str(pdf), str(target.with_suffix(""))], check=True, capture_output=True)
        samples.append({"sample_id": sid, "file": f"files/{target.name}", "sha256": sha256(target),
                        "expected": "visdoc", "split": "calibration" if sid in CALIBRATION else "validation",
                        "source": {"url": PUBLIC_PDF, "pdf_sha256": pdf_sha, "page": page}})
    if len(samples) != 12:
        raise ValueError("Expected nine original images and three document pages")
    result = {"schema_version": 1, "policy_version": "four-image-v1", "samples": samples}
    path = output / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != result:
        raise ValueError("Refusing to replace a different frozen manifest; choose a new output directory")
    (output / "files").mkdir(parents=True, exist_ok=True)
    for asset in files.iterdir():
        shutil.copyfile(asset, output / "files" / asset.name)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing-manifest", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True, help=f"Local copy of {PUBLIC_PDF}")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(prepare(args.existing_manifest, args.pdf, args.output))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"sample_preparation_failed: {type(exc).__name__}; check inputs and Poppler\n")


if __name__ == "__main__":
    main()
