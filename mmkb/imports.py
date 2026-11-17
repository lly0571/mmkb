"""Durable per-file admission, shared by HTTP directory and browser clients."""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from mmkb.db.models import ImportItem, Resource, StorageProfile
from mmkb.errors import ResourceError
from mmkb.resources import IMAGE_WORKFLOWS, ResourceService, resource_view, utcnow

logger = logging.getLogger(__name__)
PROFILE_FIELDS = ("id", "name", "backend", "local_root", "endpoint_url", "bucket", "credential_ref")


def request_fingerprint(values, duplicate_policy):
    return hashlib.sha256(json.dumps(dict(values, duplicate_policy=duplicate_policy),
                                     sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ImportService:
    def __init__(self, resources: ResourceService):
        self.resources = resources
        self.sessions = resources.sessions

    @contextmanager
    def transaction(self):
        try:
            with self.sessions.begin() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
                yield session
        except sa.exc.OperationalError as error:
            if getattr(error.orig, "pgcode", None) == "55P03":
                raise ResourceError("import_busy", "Import is in progress; resolve before retrying", 409) from None
            raise

    def options(self):
        return dict(schema_version=1, formats=["PNG", "JPEG", "WEBP"],
                    image_workflows=sorted(IMAGE_WORKFLOWS),
                    max_upload_bytes=self.resources.config.max_upload_bytes,
                    max_text_chars=self.resources.config.max_text_chars)

    @staticmethod
    def _same(item, fingerprint):
        if item.request_hash != fingerprint:
            raise ResourceError("idempotency_conflict", "Import key belongs to different content or fields", 409)

    @staticmethod
    def _result(session, item, replayed):
        resource = session.get(Resource, item.resource_id)
        if resource is None or resource.deleted_at is not None:
            raise ResourceError("import_resource_deleted", "Original import resource has been deleted", 410)
        return dict(resource_view(resource), duplicate=item.duplicate,
                    import_outcome=item.outcome, replayed=replayed)

    def create(self, key: UUID, *, duplicate_policy="create", expected_sha256=None, **kwargs):
        if duplicate_policy not in {"skip", "create"}:
            raise ResourceError("invalid_duplicate_policy", "Use skip or create")
        values, extension = self.resources.prepare(**kwargs)
        if expected_sha256 is not None and expected_sha256 != values["sha256"]:
            raise ResourceError("source_changed", "Image bytes differ from the preview", 409)
        fingerprint = request_fingerprint(values, duplicate_policy)
        # Persist the reservation before any object writes. ON CONFLICT also handles two
        # clients whose first request for the same key arrives simultaneously.
        with self.transaction() as session:
            session.execute(insert(ImportItem).values(
                key=key, request_hash=fingerprint, sha256=values["sha256"],
                planned_resource_id=uuid4(), state="reserved",
            ).on_conflict_do_nothing(index_elements=[ImportItem.key]))
            item = session.get(ImportItem, key)
            self._same(item, fingerprint)
            if item.state == "completed":
                return self._result(session, item, True)

        try:
            # Persist a stable location before storage.put, even across profile changes.
            with self.transaction() as session:
                item = session.scalar(sa.select(ImportItem).where(ImportItem.key == key).with_for_update(nowait=True))
                if item.state == "completed":
                    return self._result(session, item, True)
                self.resources.lock_content(session, values["sha256"])
                duplicate = self.resources.duplicate_resource(session, values["sha256"])
                if duplicate_policy == "skip" and duplicate is not None:
                    # A fresh skip needs no storage profile, upload or DELETE request.
                    self._remove_reserved_object(item)
                    item.resource_id, item.outcome, item.duplicate = duplicate.id, "skipped", True
                    item.state, item.error, item.updated_at = "completed", None, utcnow()
                    return self._result(session, item, False)
                if extension and item.storage_profile is None:
                    profile, object_key = self.resources.plan_storage(item.planned_resource_id, extension)
                    item.storage_profile = {name: str(getattr(profile, name)) if name == "id"
                                            else getattr(profile, name) for name in PROFILE_FIELDS}
                    item.object_key = object_key

            with self.transaction() as session:
                # NOWAIT prevents an unbounded wait behind a storage operation on this item.
                try:
                    item = session.scalar(sa.select(ImportItem).where(
                        ImportItem.key == key).with_for_update(nowait=True))
                except sa.exc.OperationalError as error:
                    if getattr(error.orig, "pgcode", None) == "55P03":
                        raise ResourceError("import_busy", "Import is in progress; resolve before retrying", 409) from None
                    raise
                if item.state == "completed":
                    return self._result(session, item, True)
                self.resources.lock_content(session, values["sha256"])
                duplicate = self.resources.duplicate_resource(session, values["sha256"])
                if duplicate_policy == "skip" and duplicate is not None:
                    # An earlier crashed attempt may have left bytes at its reserved key.
                    self._remove_reserved_object(item)
                    item.resource_id, item.outcome, item.duplicate = duplicate.id, "skipped", True
                    result = dict(resource_view(duplicate), duplicate=True)
                else:
                    profile = self._profile(item)
                    storage = self.resources.storage_resolver(profile) if profile else None
                    result = self.resources.insert(session, item.planned_resource_id, values,
                                                   kwargs.get("source"), storage, profile, item.object_key)
                    item.resource_id, item.outcome = item.planned_resource_id, "created"
                    item.duplicate = result["duplicate"]
                item.state, item.error, item.updated_at = "completed", None, utcnow()
                result.update(import_outcome=item.outcome, replayed=False)
            return result
        except ResourceError as error:
            if error.code not in {"import_busy", "import_resource_deleted"}:
                self._record_failure(key, error.code)
            raise
        except Exception:
            # A failed COMMIT acknowledgement is uncertain. Never delete a reserved original
            # here: it may already belong to a committed resource. Resolve via a fresh session.
            try:
                with self.sessions() as session:
                    item = session.get(ImportItem, key)
                    if item.state == "completed":
                        return self._result(session, item, True)
            except Exception:
                pass
            raise

    @staticmethod
    def _profile(item):
        if item.storage_profile is None:
            return None
        values = dict(item.storage_profile)
        values["id"] = UUID(values["id"])
        return StorageProfile(**values)

    def _remove_reserved_object(self, item):
        profile = self._profile(item)
        if profile and item.object_key:
            self.resources.storage_resolver(profile).delete(item.object_key)

    def _record_failure(self, key, code):
        try:
            with self.transaction() as session:
                item = session.scalar(sa.select(ImportItem).where(ImportItem.key == key).with_for_update(nowait=True))
                if item.state != "completed":
                    item.state, item.error, item.updated_at = "failed", code, utcnow()
        except Exception as error:
            logger.warning("Import failure receipt deferred key=%s error_type=%s", key, type(error).__name__)

    def resolve(self, keys):
        with self.sessions() as session:
            receipts = {item.key: item for item in session.scalars(sa.select(ImportItem).where(ImportItem.key.in_(keys)))}
            ids = {item.resource_id for item in receipts.values() if item.resource_id}
            resources = {item.id: item for item in session.scalars(sa.select(Resource).where(Resource.id.in_(ids)))} if ids else {}
            results = []
            for key in keys:
                item = receipts.get(key)
                if item is None:
                    results.append(dict(key=str(key), state="missing", resource_id=None, outcome=None,
                                        duplicate=False, resource_status=None, resource_workflow=None,
                                        resource_error=None, error=None))
                    continue
                resource = resources.get(item.resource_id)
                deleted = item.state == "completed" and (resource is None or resource.deleted_at is not None)
                results.append(dict(key=str(key), state=item.state, resource_id=item.resource_id,
                                    outcome=item.outcome, duplicate=item.duplicate, error=item.error,
                                    resource_status="deleted" if deleted else resource.status if resource else None,
                                    resource_workflow=resource.workflow if resource and not deleted else None,
                                    resource_error=resource.error if resource and not deleted else None))
            return dict(items=results)
