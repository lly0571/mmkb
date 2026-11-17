"""Real routing/encoding smoke in a fresh, automatically removed mmkb_test_* database."""
import argparse
from contextlib import closing
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from uuid import uuid4

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from mmkb.config import AppConfig, RoutingConfig, load_app_config
from mmkb.db.models import ResourceIndex
from mmkb.db.session import create_session_factory
from mmkb.processing import Processor
from mmkb.providers import build_providers
from mmkb.resources import ResourceService
from mmkb.routing_eval import load_manifest, write_json
from mmkb.search import SearchService
from mmkb.storage import StorageResolver, initialize_profile
from mmkb.worker import Worker, WorkerLock

ROOT = Path(__file__).resolve().parents[2]


def run(manifest_path: Path, output: Path, *, regression: bool = False):
    import os

    manifest, fingerprint = load_manifest(manifest_path)
    samples = {s["sample_id"]: s for s in manifest["samples"]}
    base = sa.engine.make_url(load_app_config().database_url)
    database = f"mmkb_test_routing_{uuid4().hex[:10]}"
    admin = sa.create_engine(base, isolation_level="AUTOCOMMIT", hide_parameters=True)
    created = False
    report = {"manifest_sha256": fingerprint, "cases": [], "database_cleaned": False, "passed": False}
    previous_url = os.environ.get("DATABASE_URL")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
        created = True
        url = base.set(database=database).render_as_string(hide_password=False)
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        with engine.connect() as connection:
            connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        engine.dispose()
        os.environ["DATABASE_URL"] = url
        alembic = Config(str(ROOT / "alembic.ini"))
        command.upgrade(alembic, "head")
        command.downgrade(alembic, "0002_import_items")
        command.upgrade(alembic, "head")
        command.check(alembic)
        report["migration_roundtrip"] = True
        if regression:
            output.parent.mkdir(parents=True, exist_ok=True)
            log = output.parent / "database-regression.log"
            with log.open("w") as stream:
                result = subprocess.run([
                    sys.executable, "-m", "pytest", "-q", "-ra", "tests/test_worker_integration.py",
                    "tests/test_resources.py", "tests/test_retrieval_integration.py",
                ], cwd=ROOT, env=dict(os.environ, TEST_DATABASE_URL=url, MMKB_TEST_DATABASE_URL=url),
                    stdout=stream, stderr=subprocess.STDOUT, timeout=120)
            report["regression_passed"] = result.returncode == 0
            if result.returncode:
                raise RuntimeError("Database regression failed; see the log")
        with tempfile.TemporaryDirectory(prefix="mmkb-routing-live-") as directory:
            config = AppConfig(database_url=url, storage_profile_name="routing-live",
                               local_storage_root=str(Path(directory) / "assets"))
            sessions = create_session_factory(url)
            try:
                with closing(StorageResolver(config)) as resolver, build_providers() as providers:
                    initialize_profile(sessions, config, resolver)
                    resources = ResourceService(sessions, config, resolver)
                    query = SearchService(sessions, config, client_factory=lambda: providers.embedding)
                    try:
                        with WorkerLock(sessions.kw["bind"]) as owner:
                            cases = [(sid, "systemone") for sid in (
                                "s-cpu-spec", "v-skater", "d-vscode", "doc-text", "d-rag-ui",
                            )] + [("d-llama-log", "llm")]
                            for sid, backend in cases:
                                sample = samples[sid]
                                policy = RoutingConfig(backend=backend)
                                worker = Worker(sessions, config, resolver, Processor(providers, routing_config=policy),
                                                ensure_owner=owner.check)
                                image = (manifest_path.parent / sample["file"]).read_bytes()
                                created_resource = resources.create(source=BytesIO(image), title=sid, tags=["routing-smoke", sid])
                                # A regression fixture may leave a pending resource. Drain only this owned scratch DB.
                                while resources.get(created_resource["id"])["status"] == "pending":
                                    worker.run_once()
                                detail = resources.get(created_resource["id"])
                                if detail["status"] != "ready":
                                    raise RuntimeError("Real processing failed")
                                processing = detail["extra"]["processing"]
                                route = processing["routing"]
                                assert detail["workflow"] == sample["expected"]
                                assert route["revision"] == detail["revision"]
                                assert (route["source"] == "llm" if backend == "llm"
                                        else route["source"] in {"systemone", "llm"})
                                assert processing["encoding"] == (
                                    "image_caption" if sample["expected"] in {"structured_image", "info_rich_image"} else "image_only"
                                )
                                with sessions() as session:
                                    index = session.get(ResourceIndex, created_resource["id"])
                                    # PostgreSQL enforces halfvec(2048); the returned HalfVector is not a list.
                                    assert index is not None and index.embedding is not None
                                result = query.search(source=BytesIO(image), tags=["routing-smoke", sid])
                                assert [item["resource_id"] for item in result["items"]] == [created_resource["id"]]
                                report["cases"].append({"sample_id": sid, "policy": asdict(policy),
                                                        "workflow": detail["workflow"], "routing": route,
                                                        "encoding": processing["encoding"], "warnings": processing["warnings"],
                                                        "image_query_with_sample_tag": True})
                                print(f"ready {sid} source={route['source']}", flush=True)
                    finally:
                        query.close()
            finally:
                sessions.kw["bind"].dispose()
        report["passed"] = True
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure_frames"] = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                                    for frame in traceback.extract_tb(exc.__traceback__)]
        raise
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        if created:
            with admin.connect() as connection:
                connection.execute(sa.text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                           "WHERE datname=:database AND pid<>pg_backend_pid()"), {"database": database})
                connection.execute(sa.text(f'DROP DATABASE "{database}"'))
            report["database_cleaned"] = True
        admin.dispose()
        write_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regression", action="store_true")
    args = parser.parse_args()
    try:
        report = run(args.manifest, args.output, regression=args.regression)
    except Exception as exc:
        parser.exit(1, f"routing_smoke_failed: {type(exc).__name__}; check the report and services\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]),
                      "database_cleaned": report["database_cleaned"]}))


if __name__ == "__main__":
    main()
