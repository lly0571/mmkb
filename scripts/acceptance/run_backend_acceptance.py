"""Backend joint-acceptance runner (LIVE-01..10) against a real deployment.

Drives the real HTTP API only (import / poll / search / edit / retry / delete
/ original bytes). No mock providers, no test fixtures. Tracks every created
resource ID in a state file so a failed run can still be cleaned up, and
never re-uploads after an uncertain network result without checking first.

Usage:
    python scripts/acceptance/run_backend_acceptance.py \
        --api http://127.0.0.1:8010 --run-dir docs_hide/acceptance-runs/<RUN_ID> \
        [--cases LIVE-01,LIVE-02,...] [--resume]
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

RUN_TAG_PREFIX = "acc"

PASS, FAIL, BLOCKED, SKIPPED = "PASS", "FAIL", "BLOCKED", "SKIPPED"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Acceptance:
    def __init__(self, api: str, run_dir: Path, poll_seconds: float, window: float, resume: bool):
        self.api = api.rstrip("/")
        self.run_dir = run_dir
        self.poll_seconds = poll_seconds
        self.window = window
        self.client = httpx.Client(base_url=self.api, timeout=120.0)
        manifest = json.loads((run_dir / "samples/manifest.json").read_text())
        self.samples: dict = manifest["samples"]
        self.queries: dict = json.loads((run_dir / "samples/queries.json").read_text())
        self.threshold = manifest["threshold"]
        self.run_tag = f"{RUN_TAG_PREFIX}-{run_dir.name}"
        self.state_path = run_dir / "acceptance_state.json"
        self.results_path = run_dir / "acceptance_results.json"
        self.state: dict = {"run_tag": self.run_tag, "api": self.api, "resources": {}}
        if resume and self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            assert self.state.get("run_tag") == self.run_tag, "state file belongs to another run"
        self.cases: list[dict] = []
        self._case_start = 0.0

    # ---------- plumbing ----------
    def save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))

    def case(self, case_id: str) -> None:
        self._case_start = time.time()
        self._case_id = case_id
        print(f"--- {case_id} ---", flush=True)

    def end(self, status: str, detail: str = "") -> bool:
        self.cases.append({
            "case": self._case_id, "status": status, "seconds": round(time.time() - self._case_start, 1),
            "detail": detail,
        })
        print(f"[{status}] {self._case_id} ({self.cases[-1]['seconds']}s) {detail}", flush=True)
        self._write_results()
        return status == PASS

    def _write_results(self) -> None:
        counts: dict[str, int] = {}
        # Merge with previous invocations so multi-call runs keep every case.
        cases = self.cases
        if self.results_path.exists():
            try:
                old = json.loads(self.results_path.read_text())
                done_now = {c["case"] for c in self.cases}
                cases = [c for c in old.get("cases", []) if c["case"] not in done_now] + self.cases
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        for c in cases:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        self.results_path.write_text(json.dumps({
            "run_tag": self.run_tag, "api": self.api, "cases": cases, "counts": counts,
            "resource_ids": {sid: rec["resource_id"] for sid, rec in self.state["resources"].items()},
        }, ensure_ascii=False, indent=2))

    def resource_ids(self) -> list[str]:
        return [rec["resource_id"] for rec in self.state["resources"].values()]

    # ---------- API helpers ----------
    def import_sample(self, sid: str, workflow: str | None, *, force_new: bool = False,
                      alt_key: str | None = None) -> tuple[bool, str]:
        """Import one sample; safe to re-call. Returns (ok, message)."""
        store_key = alt_key or sid
        rec = None if force_new else self.state["resources"].get(store_key)
        if rec and rec.get("resource_id"):
            r = self.client.get(f"/api/resources/{rec['resource_id']}")
            if r.status_code == 200:
                return True, "reused"
            if r.status_code != 404:
                return False, f"state id unusable, unexpected GET {r.status_code}"
            del self.state["resources"][store_key]  # stale entry, re-import
        s = self.samples[sid]
        path = self.run_dir / "samples/files" / s["file"]
        data: dict = {"title": sid, "tags": [self.run_tag, f"route-{s['route']}"]}
        if workflow:
            data["workflow"] = workflow
        files = None
        if s["format"] != "TEXT":
            files = {"file": (s["file"], path.read_bytes(), s["mime"])}
        else:
            data["user_text"] = path.read_text(encoding="utf-8")
        if s.get("user_text"):
            data["user_text"] = s["user_text"]
        try:
            r = self.client.post("/api/resources", data=data, files=files)
        except httpx.TransportError as e:
            # Network result unknown: check whether the upload actually landed before retrying.
            found = self._find_by_title(sid)
            if found:
                self.state["resources"][store_key] = {"resource_id": found["id"], "revision": found["revision"]}
                self.save_state()
                return True, f"recovered after transport error: {found['id']}"
            return False, f"transport error and not found afterwards: {e}"
        if r.status_code != 201:
            return False, f"import returned {r.status_code}: {r.text[:200]}"
        body = r.json()
        self.state["resources"][store_key] = {"resource_id": str(body["id"]), "revision": body["revision"],
                                        "created": {"status": body["status"], "duplicate": body["duplicate"],
                                                    "workflow": body["workflow"], "input_kind": body["input_kind"],
                                                    "sha256": body["sha256"]}}
        self.save_state()
        return True, f"created {body['id']}"

    def _find_by_title(self, sid: str) -> dict | None:
        r = self.client.get("/api/resources", params={"tags": self.run_tag, "limit": 100})
        if r.status_code != 200:
            return None
        for item in r.json()["items"]:
            if item["title"] == sid:
                return item
        return None

    def wait_terminal(self, sid: str) -> tuple[bool, dict | None, str]:
        rec = self.state["resources"][sid]
        deadline = time.time() + self.window
        while time.time() < deadline:
            r = self.client.get(f"/api/resources/{rec['resource_id']}")
            if r.status_code != 200:
                return False, None, f"detail GET {r.status_code}"
            body = r.json()
            if body["status"] in {"ready", "failed"}:
                rec["revision"] = body["revision"]
                rec["last_status"] = body["status"]
                self.save_state()
                return True, body, ""
            time.sleep(self.poll_seconds)
        return False, None, f"window {self.window}s exceeded, status still processing/pending"

    # ---------- cases ----------
    def live01(self) -> None:
        self.case("LIVE-01")
        problems = []
        for sid, s in self.samples.items():
            ok, msg = self.import_sample(sid, s["route"])
            if not ok:
                problems.append(f"{sid}: {msg}")
                continue
            if msg == "reused":
                continue
            rec = self.state["resources"][sid]
            created = rec["created"]
            rid = rec["resource_id"]
            d = json.loads(self.client.get(f"/api/resources/{rid}").text)
            if d["revision"] != 1 or d["status"] != "pending":
                problems.append(f"{sid}: expected revision=1/pending, got {d['revision']}/{d['status']}")
            if created["duplicate"] is not False:
                problems.append(f"{sid}: unexpected duplicate flag {created['duplicate']}")
            if s["format"] == "TEXT":
                if d["sha256"] is not None or d["original_url"] is not None:
                    problems.append(f"{sid}: text sample must have no sha256 object and no original")
            else:
                if d["sha256"] != s["sha256"] or d["original_url"] is None:
                    problems.append(f"{sid}: image original_url/sha256 mismatch")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:6]))

    def live02(self) -> None:
        self.case("LIVE-02")
        problems, notes = [], []
        for sid in self.samples:
            ok, body, msg = self.wait_terminal(sid)
            if not ok:
                problems.append(f"{sid}: {msg}")
                continue
            s = self.samples[sid]
            if body["status"] != "ready":
                problems.append(f"{sid}: ended {body['status']} error={body['error']}")
                continue
            if body["workflow"] != s["route"]:
                problems.append(f"{sid}: route {body['workflow']} != frozen {s['route']}")
            warnings = (body.get("extra", {}).get("processing", {}) or {}).get("warnings", [])
            if warnings:
                notes.append(f"{sid}: warnings={warnings}")
            if s["route"] in {"structured_image", "info_rich_image"} and not (body.get("caption") or "").strip():
                # Processing explicitly permits whole-image fallback after truncated optional
                # vision output. Require its exact warning; unexplained missing text still fails.
                if {"stage": "caption", "code": "output_truncated"} in warnings:
                    notes.append(f"{sid}: caption truncated; recorded whole-image fallback")
                else:
                    problems.append(f"{sid}: caption missing for {s['route']}")
            if s["route"] in {"visdoc", "visual_image"}:
                if body.get("caption") is not None or body.get("ocr_text") is not None:
                    problems.append(f"{sid}: image-only route must not auto-generate text")
            if body.get("caption") is not None and s["route"] == "text_image":
                problems.append(f"{sid}: text_image must not auto-caption")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:6]) or ("; ".join(notes[:8]) or "all ready, no warnings"))

    def live03(self) -> None:
        self.case("LIVE-03")
        unrouted = ["s-bench-table", "v-neon", "d-vscode"]
        problems, observed = [], {}
        for sid in unrouted:
            ok, msg = self.import_sample(sid, workflow=None, force_new=True, alt_key=f"{sid}#unrouted")
            if not ok:
                problems.append(f"{sid}: {msg}")
                continue
            alt_sid = f"{sid}#unrouted"
            ok, body, msg2 = self.wait_terminal(alt_sid)
            if not ok:
                problems.append(f"{sid}: {msg2}")
                continue
            observed[sid] = {"workflow": body["workflow"], "status": body["status"],
                             "warnings": body.get("extra", {}).get("processing", {}).get("warnings", [])}
            if body["status"] == "ready" and body["workflow"] not in {"structured_image", "info_rich_image", "visual_image", "visdoc"}:
                problems.append(f"{sid}: classifier returned invalid route {body['workflow']}")
        self.state["observations_live03"] = observed
        self.save_state()
        print("  classifier routing:", json.dumps(observed, ensure_ascii=False))
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "classifier routes recorded")

    def live04(self) -> None:
        self.case("LIVE-04")
        problems, notes = [], []
        r0 = self.client.get("/api/resources", params={"tags": self.run_tag, "limit": 100})
        total_before = r0.json()["total"]
        all_q = self.queries["text"] + self.queries["image"] + self.queries["image_text"]
        for q in all_q:
            data: dict = {"top_k": str(self.threshold["top_k"])}
            files = None
            if q["type"] == "text":
                data["text"] = q["text"]
            else:
                s = self.samples[q["image_sample"]]
                files = {"file": (s["file"], (self.run_dir / "samples/files" / s["file"]).read_bytes(), s["mime"])}
                if q["type"] == "image_text":
                    data["text"] = q["text"]
            r = self.client.post("/api/search", data=data, files=files)
            if r.status_code != 200:
                problems.append(f"{q['query_id']}: search {r.status_code} {r.text[:120]}")
                continue
            body = r.json()
            hits = body["items"]
            scores = [i["score"] for i in hits]
            if body.get("mode") != "vector":
                problems.append(f"{q['query_id']}: mode={body.get('mode')}")
            if not all(isinstance(x, float) and 0.0 < x <= 1.0 for x in scores):
                problems.append(f"{q['query_id']}: invalid scores {scores[:3]}")
            top_ids = {str(i["resource_id"]) for i in hits[: self.threshold["top_k"]]}
            hit = [s for s in q["relevant"]
                   if self.state["resources"].get(s, {}).get("resource_id") in top_ids]
            notes.append(f"{q['query_id']}: hits={hit} top={[i['title'] for i in hits[:3]]}")
            if len(hit) < self.threshold["min_relevant_hits"]:
                problems.append(f"{q['query_id']}: no relevant sample in top-{self.threshold['top_k']}")
        r = self.client.get("/api/resources", params={"tags": self.run_tag, "limit": 100})
        if r.json()["total"] != total_before:
            problems.append(f"search changed resource count: {total_before} -> {r.json()['total']}")
        print("  query results:", *notes, sep="\n    ")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or f"{len(all_q)} queries met the frozen threshold")

    def live05(self) -> None:
        self.case("LIVE-05")
        problems = []
        route_tag = f"route-structured_image"
        r = self.client.get("/api/resources", params={"tags": [self.run_tag, route_tag], "limit": 100})
        if r.status_code != 200:
            problems.append(f"tag AND list {r.status_code}")
        else:
            items = r.json()["items"]
            titles = {i["title"].split("#")[0] for i in items}
            expected = {sid for sid, s in self.samples.items() if s["route"] == "structured_image"}
            if not expected.issubset(titles):
                problems.append(f"tag AND missing {expected - titles}")
        r = self.client.get("/api/resources", params={"tags": [self.run_tag, "route-none"], "limit": 100})
        if r.status_code == 200 and r.json()["items"]:
            problems.append("no-match tag returned items")
        # search with a tag no resource carries must return empty items
        fr = self.client.post("/api/search", data={"text": "PostgreSQL 调优", "tags": "route-none", "top_k": "5"})
        if fr.status_code != 200 or fr.json()["items"]:
            problems.append(f"tag-filtered search should return empty items, got {fr.status_code}")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "AND filter + empty result OK")

    def live06(self) -> None:
        self.case("LIVE-06")
        problems = []
        for sid, s in self.samples.items():
            rec = self.state["resources"][sid]
            r = self.client.get(f"/api/resources/{rec['resource_id']}/original")
            if s["format"] == "TEXT":
                if r.status_code != 404:
                    problems.append(f"{sid}: text original must 404, got {r.status_code}")
                continue
            if r.status_code != 200:
                problems.append(f"{sid}: original {r.status_code}")
                continue
            if sha256_of(r.content) != s["sha256"]:
                problems.append(f"{sid}: original bytes sha256 mismatch")
        # unrouted LIVE-03 copies too
        for alt in list(self.state["resources"]):
            if "#" in alt:
                rec = self.state["resources"][alt]
                r = self.client.get(f"/api/resources/{rec['resource_id']}/original")
                src = self.samples[alt.split("#")[0]]
                if r.status_code != 200 or sha256_of(r.content) != src["sha256"]:
                    problems.append(f"{alt}: original mismatch")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "all originals verified")

    def live07(self) -> None:
        self.case("LIVE-07")
        problems = []
        # text resource: user_text + tags edit
        sid = "n-db-tuning"
        rec = self.state["resources"][sid]
        rev = rec["revision"]
        r = self.client.patch(f"/api/resources/{rec['resource_id']}",
                              json={"revision": rev, "user_text": "修订后的 PostgreSQL 慢查询调优笔记：新增 pg_stat_statements 与 pgbouncer 说明。",
                                    "tags": [self.run_tag, f"route-text", "edited"]})
        if r.status_code != 200 or r.json()["revision"] != rev + 1:
            problems.append(f"edit text: {r.status_code} {r.text[:150]}")
        else:
            ok, body, msg = self.wait_terminal(sid)
            if not ok or body["status"] != "ready":
                problems.append(f"edit text reprocess: {msg or body['status']}")
            elif "pgbouncer" not in (body.get("user_text") or ""):
                problems.append("edited user_text not visible after reprocess")
        # visdoc manual OCR preserved through reprocess
        sid = "d-vscode"
        rec = self.state["resources"][sid]
        rev = rec["revision"]
        r = self.client.patch(f"/api/resources/{rec['resource_id']}",
                              json={"revision": rev, "ocr_text": "人工核对：Accuracy 0.6875，Precision 0.6944。", "title": "vscode-eval-terminal-edited"})
        if r.status_code != 200 or r.json()["revision"] != rev + 1:
            problems.append(f"edit visdoc: {r.status_code} {r.text[:150]}")
        else:
            ok, body, msg = self.wait_terminal(sid)
            if not ok or body["status"] != "ready":
                problems.append(f"edit visdoc reprocess: {msg or body['status']}")
            elif body.get("ocr_text") != "人工核对：Accuracy 0.6875，Precision 0.6944。":
                problems.append(f"manual ocr_text not preserved: {body.get('ocr_text')!r}")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "edits reprocessed with revision increments")

    def live08(self) -> None:
        self.case("LIVE-08")
        problems, notes = [], []
        # empty string must be preserved and not regenerated
        sid = "d-vscode"
        rec = self.state["resources"][sid]
        r = self.client.patch(f"/api/resources/{rec['resource_id']}", json={"revision": rec["revision"], "ocr_text": ""})
        if r.status_code != 200:
            problems.append(f"empty ocr patch {r.status_code}")
        else:
            ok, body, msg = self.wait_terminal(sid)
            if not ok or body["status"] != "ready":
                problems.append(f"empty ocr reprocess: {msg or body['status']}")
            elif body.get("ocr_text") != "":
                problems.append(f"empty string not preserved: {body.get('ocr_text')!r}")
            else:
                notes.append("empty ocr_text preserved, no regeneration")
        # explicit null on a route that needs it -> regenerated
        sid = "s-cpu-spec"
        rec = self.state["resources"][sid]
        r = self.client.patch(f"/api/resources/{rec['resource_id']}", json={"revision": rec["revision"], "caption": None})
        if r.status_code != 200:
            problems.append(f"null caption patch {r.status_code}")
        else:
            ok, body, msg = self.wait_terminal(sid)
            if not ok or body["status"] != "ready":
                problems.append(f"null caption reprocess: {msg or body['status']}")
            elif not (body.get("caption") or "").strip():
                problems.append("caption not regenerated after null")
            else:
                notes.append("structured_image caption regenerated after explicit null")
        # text_image must not auto-analyze
        sid = "t-selfie"
        rec = self.state["resources"][sid]
        r = self.client.patch(f"/api/resources/{rec['resource_id']}", json={"revision": rec["revision"], "caption": None, "ocr_text": None})
        if r.status_code != 200:
            problems.append(f"text_image null patch {r.status_code}")
        else:
            ok, body, msg = self.wait_terminal(sid)
            if not ok or body["status"] != "ready":
                problems.append(f"text_image reprocess: {msg or body['status']}")
            elif body.get("caption") is not None or body.get("ocr_text") is not None:
                problems.append(f"text_image auto-analyzed: caption={body.get('caption')!r} ocr={body.get('ocr_text')!r}")
            else:
                notes.append("text_image stayed without auto analysis")
        print("  ", *notes, sep="\n  ")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "; ".join(notes))

    def live09(self) -> None:
        self.case("LIVE-09")
        problems = []
        sid = "n-rustfs"
        rec = self.state["resources"][sid]
        cur = self.client.get(f"/api/resources/{rec['resource_id']}").json()
        rec["revision"] = cur["revision"]  # always trust the server for this case
        if rec["revision"] < 2:
            # create a real second revision so a stale revision exists at all
            r = self.client.patch(f"/api/resources/{rec['resource_id']}",
                                  json={"revision": rec["revision"], "title": "n-rustfs-r2"})
            if r.status_code != 200:
                problems.append(f"revision bump failed: {r.status_code}")
            else:
                ok, body, msg = self.wait_terminal(sid)
                if not ok:
                    problems.append(f"revision bump reprocess: {msg}")
        rec = self.state["resources"][sid]
        current_title = self.client.get(f"/api/resources/{rec['resource_id']}").json()["title"]
        stale = max(1, rec["revision"] - 1)
        r = self.client.patch(f"/api/resources/{rec['resource_id']}", json={"revision": stale, "title": "stale-edit"})
        if r.status_code != 409:
            problems.append(f"stale PATCH expected 409, got {r.status_code}")
        r = self.client.get(f"/api/resources/{rec['resource_id']}")
        if r.status_code == 200 and r.json()["title"] == "stale-edit":
            problems.append("stale edit overwrote content")
        if r.status_code == 200 and r.json()["title"] != current_title:
            problems.append("content changed after stale attempt")
        r = self.client.post(f"/api/resources/{rec['resource_id']}/retry", json={"revision": stale})
        if r.status_code != 409:
            problems.append(f"stale retry expected 409, got {r.status_code}")
        r = self.client.post(f"/api/resources/{rec['resource_id']}/retry", json={"revision": rec["revision"]})
        if r.status_code not in {409, 422}:
            problems.append(f"retry on ready resource expected 4xx, got {r.status_code}")
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "stale edits/retries rejected with 409")

    def live10(self, verify_s3: bool) -> None:
        self.case("LIVE-10")
        problems, notes = [], []
        ids = self.resource_ids()
        for rid in ids:
            r = self.client.delete(f"/api/resources/{rid}")
            if r.status_code != 204:
                problems.append(f"delete {rid} -> {r.status_code}")
        for rid in ids[:3]:
            r = self.client.delete(f"/api/resources/{rid}")
            if r.status_code != 204:
                problems.append(f"repeat delete {rid} -> {r.status_code}")
        for sid, rec in list(self.state["resources"].items()):
            rid = rec["resource_id"]
            if self.client.get(f"/api/resources/{rid}").status_code != 404:
                problems.append(f"{sid}: detail still visible")
            orig = self.client.get(f"/api/resources/{rid}/original")
            if orig.status_code != 404:
                problems.append(f"{sid}: original still visible ({orig.status_code})")
        r = self.client.get("/api/resources", params={"tags": self.run_tag, "limit": 100})
        if r.json()["total"] != 0:
            problems.append(f"list still shows {r.json()['total']} resources")
        fr = self.client.post("/api/search", data={"text": "PostgreSQL 慢查询", "tags": self.run_tag, "top_k": "5"})
        if fr.status_code == 200 and fr.json()["items"]:
            problems.append("search still returns deleted resources")
        if verify_s3:
            deleted_keys, remaining = self._s3_objects()
            notes.append(f"RustFS objects remaining after cleanup: {len(remaining)}")
            if remaining:
                problems.append(f"RustFS objects not removed: {remaining[:5]}")
        self.state["deleted"] = True
        self.save_state()
        return self.end(FAIL if problems else PASS, "; ".join(problems[:5]) or "; ".join(notes) or "deleted everywhere, bucket clean")

    def _s3_objects(self) -> tuple[list[str], list[str]]:
        import boto3
        from botocore.client import Config
        cp = configparser.ConfigParser()
        cp.read(os.path.expanduser("~/.aws/credentials"))
        profile = os.environ.get("S3_CREDENTIAL_PROFILE", "rustfs")
        s3 = boto3.client("s3",
                          endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
                          aws_access_key_id=cp[profile]["aws_access_key_id"],
                          aws_secret_access_key=cp[profile]["aws_secret_access_key"],
                          region_name=os.environ.get("S3_REGION", "us-east-1"),
                          config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"))
        bucket = os.environ["S3_BUCKET"]
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])]
        return keys, keys


CASES = ["LIVE-01", "LIVE-02", "LIVE-03", "LIVE-04", "LIVE-05", "LIVE-06", "LIVE-07", "LIVE-08", "LIVE-09", "LIVE-10"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("MMKB_ACCEPTANCE_API", "http://127.0.0.1:8010"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", default=",".join(CASES))
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--window", type=float, default=600.0)
    parser.add_argument("--no-s3-verify", action="store_true", help="skip direct RustFS object checks in LIVE-10")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    acc = Acceptance(args.api, args.run_dir, args.poll_seconds, args.window, args.resume)
    print(f"API {acc.api} | run {acc.run_tag} | {len(acc.samples)} samples, "
          f"{sum(len(v) for v in acc.queries.values())} queries", flush=True)
    selected = [c.strip() for c in args.cases.split(",") if c.strip()]
    ok = True
    try:
        for case_id in selected:
            if case_id == "LIVE-01":
                ok &= acc.live01()
            elif case_id == "LIVE-02":
                ok &= acc.live02()
            elif case_id == "LIVE-03":
                ok &= acc.live03()
            elif case_id == "LIVE-04":
                ok &= acc.live04()
            elif case_id == "LIVE-05":
                ok &= acc.live05()
            elif case_id == "LIVE-06":
                ok &= acc.live06()
            elif case_id == "LIVE-07":
                ok &= acc.live07()
            elif case_id == "LIVE-08":
                ok &= acc.live08()
            elif case_id == "LIVE-09":
                ok &= acc.live09()
            elif case_id == "LIVE-10":
                ok &= acc.live10(verify_s3=not args.no_s3_verify)
            else:
                raise SystemExit(f"unknown case {case_id}")
    finally:
        acc.save_state()
    counts: dict[str, int] = {}
    for c in acc.cases:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    print("summary:", counts, "| resource ids kept in", acc.state_path, flush=True)
    return 0 if ok and all(c["status"] == PASS for c in acc.cases) else 1


if __name__ == "__main__":
    sys.exit(main())
