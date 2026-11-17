"""Resource lifecycle services, independent of HTTP and model providers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
from typing import BinaryIO
from uuid import UUID, uuid4

import sqlalchemy as sa

from mmkb.config import AppConfig, load_app_config
from mmkb.db.models import Resource, ResourceIndex, StorageProfile
from mmkb.db.session import create_session_factory
from mmkb.errors import ResourceError, StorageError
from mmkb.images import inspect_image
from mmkb.storage import StorageResolver, validate_key

logger = logging.getLogger(__name__)
IMAGE_WORKFLOWS = {"structured_image", "visual_image", "visdoc"}
STATUSES = {"pending", "processing", "ready", "failed"}
INPUT_KINDS = {"image", "text_image", "text"}
EDIT_FIELDS = {"title", "tags", "user_text", "caption", "ocr_text", "workflow"}


def utcnow():
    return datetime.now(timezone.utc)


def normalize_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ResourceError("invalid_tags", "Tags must be a list of strings")
    return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


def input_route(has_image: bool, text: str | None, workflow: str | None):
    has_text = bool(text and text.strip())
    if not has_image and not has_text:
        raise ResourceError("empty_input", "Provide an image or non-empty text")
    kind = "text_image" if has_image and has_text else "image" if has_image else "text"
    if kind == "image":
        if workflow is not None and workflow not in IMAGE_WORKFLOWS:
            raise ResourceError("invalid_workflow", "Workflow is incompatible with image input")
    elif workflow is not None and workflow != kind:
        raise ResourceError("invalid_workflow", "Workflow is incompatible with input")
    return kind, workflow if kind == "image" else kind


def resource_view(resource: Resource) -> dict:
    result = {name: getattr(resource, name) for name in (
        "id", "input_kind", "workflow", "title", "tags", "user_text", "caption", "ocr_text",
        "extra", "sha256", "status", "error", "revision", "created_at", "updated_at",
    )}
    result["original_url"] = f"/api/resources/{resource.id}/original" if resource.object_key else None
    return result


class ResourceService:
    def __init__(self, sessions, config: AppConfig, storage_resolver=None):
        self.sessions = sessions
        self.config = config
        self.storage_resolver = storage_resolver or StorageResolver(config)

    def _validate_texts(self, values: dict):
        for key in ("title", "user_text", "caption", "ocr_text"):
            value = values.get(key)
            if value is not None and not isinstance(value, str):
                raise ResourceError("invalid_text", f"{key} must be text or null")
            if value is not None and len(value) > self.config.max_text_chars:
                raise ResourceError("text_too_large", f"{key} exceeds MAX_TEXT_CHARS", 413)
        if "title" in values and values["title"] is None:
            raise ResourceError("invalid_title", "Title cannot be null")

    def _inspect_image(self, source: BinaryIO, filename: str | None):
        return inspect_image(source, filename, max_upload_bytes=self.config.max_upload_bytes)

    @staticmethod
    def _get(session, resource_id: UUID, lock=False):
        query = sa.select(Resource).where(Resource.id == resource_id, Resource.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        resource = session.scalar(query)
        if resource is None:
            raise ResourceError("resource_not_found", "Resource not found", 404)
        return resource

    @staticmethod
    def _check_revision(resource, revision):
        if revision != resource.revision:
            raise ResourceError("revision_conflict", "Resource changed; reload and retry with the current revision", 409)

    @staticmethod
    def _invalidate(session, resource):
        session.execute(sa.delete(ResourceIndex).where(ResourceIndex.resource_id == resource.id))
        resource.revision += 1
        resource.updated_at = utcnow()
        resource.error = None
        resource.status = "pending"

    def create(self, *, source: BinaryIO | None = None, filename: str | None = None,
               title: str = "", tags: list[str] | None = None, user_text: str | None = None,
               workflow: str | None = None) -> dict:
        values, extension = self.prepare(source=source, filename=filename, title=title,
                                         tags=tags, user_text=user_text, workflow=workflow)
        resource_id = uuid4()
        profile, key = self.plan_storage(resource_id, extension)
        storage = self.storage_resolver(profile) if profile else None
        try:
            with self.sessions.begin() as session:
                self.lock_content(session, values["sha256"])
                result = self.insert(session, resource_id, values, source, storage, profile, key)
            logger.info("Resource created resource_id=%s revision=1", resource_id)
            return result
        except Exception:
            # COMMIT can succeed even when its acknowledgement is lost. Reacquire the same
            # content lock and prove absence using a fresh connection before deleting bytes.
            if storage:
                try:
                    with self.sessions() as session, session.begin():
                        self.lock_content(session, values["sha256"])
                        if session.get(Resource, resource_id) is None:
                            storage.delete(key)
                except Exception as cleanup_error:
                    logger.error("Import compensation deferred resource_id=%s error_type=%s",
                                 resource_id, type(cleanup_error).__name__)
            raise

    def prepare(self, *, source=None, filename=None, title="", tags=None, user_text=None,
                workflow=None):
        """Validate once, preserving the exact stream later written to storage."""
        self._validate_texts(dict(title=title, user_text=user_text))
        tags = normalize_tags(tags or [])
        user_text = user_text if user_text and user_text.strip() else None
        kind, workflow = input_route(source is not None, user_text, workflow)
        sha256 = None
        extension = None
        extra = {}
        if source is not None:
            sha256, extension, extra = self._inspect_image(source, filename)
        return dict(input_kind=kind, workflow=workflow, title=title, tags=tags,
                    user_text=user_text, extra=extra, sha256=sha256), extension

    @staticmethod
    def lock_content(session, sha256):
        if sha256:
            # The one-bigint space does not overlap the Worker's two-int lock space.
            lock_key = int.from_bytes(bytes.fromhex(sha256)[:8], "big", signed=True)
            acquired = session.scalar(sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key})
            if not acquired:
                raise ResourceError("import_busy", "Content is being imported; resolve and retry", 409)

    @staticmethod
    def duplicate_resource(session, sha256):
        if not sha256:
            return None
        return session.scalar(sa.select(Resource).where(
            Resource.sha256 == sha256, Resource.deleted_at.is_(None),
        ).order_by(Resource.created_at, Resource.id).limit(1))

    def plan_storage(self, resource_id, extension):
        if extension is None:
            return None, None
        with self.sessions() as session:
            profile = session.scalar(sa.select(StorageProfile).where(
                StorageProfile.name == self.config.storage_profile_name))
            if profile is None:
                raise StorageError("Storage profile not initialized; run python -m mmkb.storage init")
            prefix = self.config.s3_prefix.rstrip("/") if profile.backend == "s3" else ""
        return profile, validate_key(f"{prefix + '/' if prefix else ''}{resource_id}/original.{extension}")

    def insert(self, session, resource_id, values, source, storage, profile, key):
        duplicate = self.duplicate_resource(session, values["sha256"]) is not None
        if storage:
            source.seek(0)
            storage.put(key, source, values["extra"]["mime_type"])
        resource = Resource(id=resource_id, **values, storage_profile_id=profile.id if profile else None,
                            object_key=key, status="pending", revision=1)
        session.add(resource)
        session.flush()
        return dict(resource_view(resource), duplicate=duplicate)

    def get(self, resource_id: UUID) -> dict:
        with self.sessions() as session:
            return resource_view(self._get(session, resource_id))

    def list(self, *, limit=20, offset=0, status=None, input_kind=None, tags=None) -> dict:
        if limit < 1 or limit > 100 or offset < 0:
            raise ResourceError("invalid_pagination", "Use limit 1..100 and nonnegative offset")
        if status is not None and status not in STATUSES:
            raise ResourceError("invalid_status", "Unknown resource status")
        if input_kind is not None and input_kind not in INPUT_KINDS:
            raise ResourceError("invalid_input_kind", "Unknown input kind")
        conditions = [Resource.deleted_at.is_(None)]
        if status is not None:
            conditions.append(Resource.status == status)
        if input_kind is not None:
            conditions.append(Resource.input_kind == input_kind)
        if tags:
            conditions.append(Resource.tags.contains(normalize_tags(tags)))
        with self.sessions() as session:
            total = session.scalar(sa.select(sa.func.count()).select_from(Resource).where(*conditions))
            items = session.scalars(sa.select(Resource).where(*conditions).order_by(
                Resource.created_at.desc(), Resource.id.desc()).limit(limit).offset(offset))
            return dict(items=[resource_view(item) for item in items], total=total, limit=limit, offset=offset)

    def update(self, resource_id: UUID, revision: int, changes: dict) -> dict:
        if changes.keys() - EDIT_FIELDS:
            raise ResourceError("invalid_fields", "Unsupported resource fields")
        self._validate_texts(changes)
        changes = dict(changes)
        if "tags" in changes:
            changes["tags"] = normalize_tags(changes["tags"])
        with self.sessions.begin() as session:
            resource = self._get(session, resource_id, lock=True)
            self._check_revision(resource, revision)
            text = changes.get("user_text", resource.user_text)
            text = text if text and text.strip() else None
            # Changing between image and text_image resets the old route automatically.
            previous_route = resource.workflow
            if resource.object_key and bool(text) != (resource.input_kind == "text_image"):
                previous_route = None
            kind, route = input_route(bool(resource.object_key), text, changes.get("workflow", previous_route))
            for key, value in changes.items():
                setattr(resource, key, value)
            resource.user_text, resource.input_kind, resource.workflow = text, kind, route
            self._invalidate(session, resource)
            session.flush()
            result = resource_view(resource)
        logger.info("Resource edited resource_id=%s revision=%s", resource_id, result["revision"])
        return result

    def retry(self, resource_id: UUID, revision: int) -> dict:
        with self.sessions.begin() as session:
            resource = self._get(session, resource_id, lock=True)
            self._check_revision(resource, revision)
            if resource.status != "failed":
                raise ResourceError("invalid_state", "Only failed resources can be retried", 409)
            self._invalidate(session, resource)
            session.flush()
            result = resource_view(resource)
        logger.info("Resource retried resource_id=%s revision=%s", resource_id, result["revision"])
        return result

    def delete(self, resource_id: UUID):
        with self.sessions.begin() as session:
            resource = session.scalar(sa.select(Resource).where(Resource.id == resource_id).with_for_update())
            if resource is None:
                return
            if resource.deleted_at is None:
                resource.deleted_at = utcnow()
                resource.updated_at = resource.deleted_at
                resource.revision += 1
                resource.error = None
                session.execute(sa.delete(ResourceIndex).where(ResourceIndex.resource_id == resource_id))
        logger.info("Resource deleted resource_id=%s", resource_id)
        self.cleanup_deleted(resource_id)

    def cleanup_deleted(self, resource_id: UUID) -> bool:
        try:
            with self.sessions() as session:
                resource = session.get(Resource, resource_id)
                if resource is None or resource.deleted_at is None:
                    return True
                key = resource.object_key
                profile = resource.storage_profile if key else None
            storage = self.storage_resolver(profile) if key else None
            if storage:
                storage.delete(key)
            with self.sessions.begin() as session:
                session.execute(sa.delete(Resource).where(Resource.id == resource_id, Resource.deleted_at.is_not(None)))
            return True
        except ResourceError as exc:
            with self.sessions.begin() as session:
                session.execute(sa.update(Resource).where(Resource.id == resource_id, Resource.deleted_at.is_not(None)).values(
                    error=f"Cleanup failed: {exc.code}", updated_at=utcnow()))
            logger.warning("Cleanup failed resource_id=%s error_type=%s", resource_id, exc.code)
            return False

    def cleanup_batch(self, limit=100) -> dict:
        with self.sessions() as session:
            ids = session.scalars(sa.select(Resource.id).where(Resource.deleted_at.is_not(None)).order_by(
                Resource.updated_at, Resource.id).limit(limit)).all()
        cleaned = sum(self.cleanup_deleted(resource_id) for resource_id in ids)
        return dict(scanned=len(ids), cleaned=cleaned, failed=len(ids) - cleaned)

    def original(self, resource_id: UUID):
        with self.sessions() as session:
            resource = self._get(session, resource_id)
            if not resource.object_key:
                raise ResourceError("original_not_found", "Text resources have no original file", 404)
            key = resource.object_key
            profile = resource.storage_profile
            mime = resource.extra.get("mime_type", "application/octet-stream")
        # No DB connection is held while the object is opened or streamed.
        storage = self.storage_resolver(profile)
        return storage.open(key), mime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["cleanup-deleted"])
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    sessions = resolver = None
    try:
        config = load_app_config()
        sessions = create_session_factory(config.database_url)
        resolver = StorageResolver(config)
        result = ResourceService(sessions, config, resolver).cleanup_batch(args.limit)
        print(result)
        if result["failed"]:
            parser.exit(1)
    except ResourceError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except sa.exc.SQLAlchemyError:
        parser.exit(1, "database_unavailable: Check database connection and migrations\n")
    except ValueError:
        parser.exit(1, "app_config: Check resource management environment configuration\n")
    finally:
        if resolver is not None:
            resolver.close()
        if sessions is not None:
            sessions.kw["bind"].dispose()


if __name__ == "__main__":
    main()
