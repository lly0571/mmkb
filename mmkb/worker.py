"""Single-instance processing Worker. Run with python -m mmkb.worker."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import math
import signal
from threading import Event
import time

import sqlalchemy as sa

from mmkb.config import AppConfig, load_app_config
from mmkb.db.models import Resource, ResourceIndex, StorageProfile
from mmkb.db.session import create_session_factory
from mmkb.embedding import EmbeddingError
from mmkb.errors import ResourceError
from mmkb.processing import Processor, ProcessingResult, ResourceSnapshot
from mmkb.providers import ImageInput, ProviderError, build_providers
from mmkb.resources import ResourceService, utcnow
from mmkb.storage import StorageResolver

logger = logging.getLogger(__name__)
# Application namespace "MMKB", singleton slot 1; shared by run and recovery.
LOCK_KEYS = {"namespace": 0x4D4D4B42, "slot": 1}


class WorkerLock:
    """Pin one autocommit connection; never reconnect after ownership is lost."""

    def __init__(self, engine):
        self.engine = engine
        self.connection = None
        self.backend_pid = None
        self.acquired = False

    def __enter__(self):
        self.connection = self.engine.connect()
        try:
            self.connection = self.connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired = self.connection.scalar(sa.text(
                "SELECT pg_try_advisory_lock(:namespace, :slot)"), LOCK_KEYS)
            if not acquired:
                raise ResourceError("worker_running", "Another Worker owns this database; stop it before recovery", 409)
            self.acquired = True
            self.backend_pid = self.connection.scalar(sa.text("SELECT pg_backend_pid()"))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def check(self):
        if self.connection is None or self.connection.closed or self.connection.invalidated:
            raise ResourceError("worker_lock_lost", "Worker ownership lost; stop and recover processing resources", 503)
        # A checked-out Connection is retained, not checked back into the pool.
        # A failed ping propagates; no rollback/reconnect/reacquire is attempted.
        if self.connection.scalar(sa.text("SELECT pg_backend_pid()")) != self.backend_pid:
            raise ResourceError("worker_lock_lost", "Worker database session changed; recover processing resources", 503)

    def __exit__(self, *args):
        if self.connection is not None:
            try:
                if self.acquired and not self.connection.invalidated:
                    self.connection.execute(sa.text("SELECT pg_advisory_unlock(:namespace, :slot)"), LOCK_KEYS)
            except sa.exc.SQLAlchemyError:
                # Do not return a possibly locked session to the connection pool.
                self.connection.invalidate()
            finally:
                self.connection.close()
                self.connection = None
                self.acquired = False


@dataclass(frozen=True)
class ClaimedResource:
    snapshot: ResourceSnapshot
    object_key: str | None
    profile: StorageProfile | None
    mime_type: str | None


def safe_error(error: Exception) -> str:
    if isinstance(error, ProviderError):
        return f"{error.capability}: {error.code}; check model service and input, then retry"
    if isinstance(error, EmbeddingError):
        return f"embedding: {error.code}; check model service and input, then retry"
    if isinstance(error, ResourceError):
        return f"{error.code}: check resource input and storage, then retry"
    return "processing_error: unexpected processing failure; check server logs, then retry"


class Worker:
    """Business operations; the runner holds WorkerLock around their lifetime."""

    def __init__(self, sessions, config: AppConfig, resolver, processor: Processor | None, *, ensure_owner=None):
        self.sessions = sessions
        self.config = config
        self.resolver = resolver
        self.processor = processor
        self.ensure_owner = ensure_owner or (lambda: None)
        self.resources = ResourceService(sessions, config, resolver)

    def claim(self) -> ClaimedResource | None:
        self.ensure_owner()
        with self.sessions.begin() as session:
            resource = session.scalar(sa.select(Resource).where(
                Resource.status == "pending", Resource.deleted_at.is_(None),
            ).order_by(Resource.created_at, Resource.id).limit(1).with_for_update())
            if resource is None:
                return None
            snapshot = ResourceSnapshot(
                resource.id, resource.revision, resource.input_kind, resource.workflow,
                resource.title, tuple(resource.tags), resource.user_text, resource.caption, resource.ocr_text,
            )
            profile = None
            if resource.object_key and resource.storage_profile is not None:
                stored = resource.storage_profile
                profile = StorageProfile(**{name: getattr(stored, name) for name in (
                    "id", "name", "backend", "local_root", "endpoint_url", "bucket", "credential_ref",
                )})
            claimed = ClaimedResource(snapshot, resource.object_key, profile, resource.extra.get("mime_type"))
            resource.status, resource.error, resource.updated_at = "processing", None, utcnow()
        logger.info("worker stage=claimed resource_id=%s revision=%s", snapshot.resource_id, snapshot.revision)
        return claimed

    @staticmethod
    def _current(session, resource_id, revision):
        return session.scalar(sa.select(Resource).where(
            Resource.id == resource_id, Resource.revision == revision,
            Resource.status == "processing", Resource.deleted_at.is_(None),
        ).with_for_update())

    def complete(self, result: ProcessingResult) -> bool:
        self.ensure_owner()
        with self.sessions.begin() as session:
            resource = self._current(session, result.resource_id, result.revision)
            if resource is None:
                return False
            index = session.get(ResourceIndex, result.resource_id)
            if index is None:
                index = ResourceIndex(resource_id=result.resource_id)
                session.add(index)
            index.embedding = result.embedding
            index.segmented_text = result.segmented_text
            index.indexed_at = utcnow()
            resource.workflow = result.workflow
            resource.caption, resource.ocr_text = result.caption, result.ocr_text
            extra = dict(resource.extra)
            previous = extra.get("processing")
            metadata = dict(previous) if isinstance(previous, dict) else {}
            metadata["warnings"] = result.warnings
            extra["processing"] = metadata
            resource.extra = extra
            resource.status, resource.error, resource.updated_at = "ready", None, utcnow()
        return True

    def fail(self, snapshot: ResourceSnapshot, error: Exception) -> bool:
        self.ensure_owner()
        with self.sessions.begin() as session:
            resource = self._current(session, snapshot.resource_id, snapshot.revision)
            if resource is None:
                return False
            session.execute(sa.delete(ResourceIndex).where(ResourceIndex.resource_id == resource.id))
            resource.status, resource.error, resource.updated_at = "failed", safe_error(error), utcnow()
        return True

    def recover_processing(self) -> int:
        self.ensure_owner()
        with self.sessions.begin() as session:
            resources = session.scalars(sa.select(Resource).where(
                Resource.status == "processing", Resource.deleted_at.is_(None),
            ).with_for_update()).all()
            for resource in resources:
                session.execute(sa.delete(ResourceIndex).where(ResourceIndex.resource_id == resource.id))
                resource.revision += 1
                resource.status, resource.error, resource.updated_at = "pending", None, utcnow()
            return len(resources)

    def run_once(self) -> dict:
        if self.processor is None:
            raise RuntimeError("A processor is required to run the Worker")
        claimed = self.claim()
        outcome = "idle"
        if claimed is not None:
            snapshot = claimed.snapshot
            started = time.monotonic()
            try:
                image = None
                if claimed.object_key:
                    if claimed.profile is None:
                        raise ResourceError("storage_config", "Resource storage profile is missing", 503)
                    with self.resolver(claimed.profile).open(claimed.object_key) as stream:
                        data = stream.read(self.config.max_upload_bytes + 1)
                    if len(data) > self.config.max_upload_bytes:
                        raise ResourceError("file_too_large", "Original exceeds MAX_UPLOAD_BYTES", 413)
                    image = ImageInput(data, claimed.mime_type)
                result = self.processor.process(snapshot, image=image)
            except sa.exc.SQLAlchemyError:
                raise
            except Exception as exc:
                outcome = "failed" if self.fail(snapshot, exc) else "stale"
                logger.warning("worker stage=processing resource_id=%s revision=%s error_type=%s",
                               snapshot.resource_id, snapshot.revision, type(exc).__name__)
            else:
                outcome = "ready" if self.complete(result) else "stale"
            logger.info("worker stage=finished resource_id=%s revision=%s outcome=%s elapsed_seconds=%.3f",
                        snapshot.resource_id, snapshot.revision, outcome, time.monotonic() - started)
        self.ensure_owner()
        cleanup = self.resources.cleanup_batch(100)
        return {"claimed": int(claimed is not None), "outcome": outcome, "cleanup": cleanup}

    def run(self, stop: Event, *, poll_seconds: float = 2.0):
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        while not stop.is_set():
            result = self.run_once()
            if not result["claimed"]:
                stop.wait(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--poll-seconds", type=float, default=2.0)
    commands.add_parser("recover-processing", help="Stop the old Worker before explicitly requeuing interrupted work")
    args = parser.parse_args()
    if args.command == "run" and (not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0):
        parser.error("--poll-seconds must be finite and positive")
    sessions = resolver = providers = None
    handlers = {}
    try:
        config = load_app_config()
        sessions = create_session_factory(config.database_url)
        resolver = StorageResolver(config)
        with WorkerLock(sessions.kw["bind"]) as guard:
            processor = None
            if args.command == "run":
                providers = build_providers()
                processor = Processor(providers, max_text_chars=config.max_text_chars)
            worker = Worker(sessions, config, resolver, processor, ensure_owner=guard.check)
            if args.command == "recover-processing":
                print(json.dumps({"requeued": worker.recover_processing()}))
            else:
                stop = Event()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    handlers[signum] = signal.signal(signum, lambda *_: stop.set())
                if args.once:
                    result = worker.run_once()
                    print(json.dumps(result))
                    if result["outcome"] == "failed" or result["cleanup"]["failed"]:
                        parser.exit(1)
                else:
                    worker.run(stop, poll_seconds=args.poll_seconds)
    except ResourceError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except sa.exc.SQLAlchemyError:
        parser.exit(1, "database_unavailable: Check database connection and migrations; recover interrupted processing after stopping the old Worker\n")
    except ValueError:
        parser.exit(1, "worker_config: Check database, storage and model configuration\n")
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        try:
            if providers is not None:
                providers.close()
        finally:
            try:
                if resolver is not None:
                    resolver.close()
            finally:
                if sessions is not None:
                    sessions.kw["bind"].dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
