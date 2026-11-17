"""FastAPI resource API. Run: uvicorn mmkb.api:create_app --factory"""
from contextlib import asynccontextmanager
from datetime import datetime
import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa
from starlette.exceptions import HTTPException

from mmkb.config import AppConfig, health_config_from_app, load_app_config
from mmkb.db.session import create_session_factory
from mmkb.errors import ResourceError
from mmkb.health import HealthReport, HealthService
from mmkb.health_runner import SubprocessProbeRunner
from mmkb.imports import ImportService
from mmkb.image_types import Workflow
from mmkb.resources import ResourceService
from mmkb.search import SearchService
from mmkb.storage import StorageResolver

logger = logging.getLogger(__name__)
Status = Literal["pending", "processing", "ready", "failed"]
InputKind = Literal["image", "text_image", "text"]


class ResourceDetail(BaseModel):
    id: UUID
    input_kind: InputKind
    workflow: Workflow | None
    title: str
    tags: list[str]
    user_text: str | None
    caption: str | None
    ocr_text: str | None
    extra: dict[str, Any]
    sha256: str | None
    status: Status
    error: str | None
    revision: int
    created_at: datetime
    updated_at: datetime
    original_url: str | None


class CreatedResource(ResourceDetail):
    duplicate: bool
    import_outcome: Literal["created", "skipped"] | None = None
    replayed: bool | None = None


class ImportOptions(BaseModel):
    schema_version: Literal[1]
    formats: list[str]
    image_workflows: list[str]
    max_upload_bytes: int
    max_text_chars: int


class ImportResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[UUID] = Field(min_length=1, max_length=100)


class ImportReceipt(BaseModel):
    key: UUID
    state: Literal["missing", "reserved", "completed", "failed"]
    resource_id: UUID | None
    outcome: Literal["created", "skipped"] | None
    duplicate: bool
    error: str | None
    resource_status: Literal["pending", "processing", "ready", "failed", "deleted"] | None
    resource_workflow: Workflow | None
    resource_error: str | None


class ImportResolution(BaseModel):
    items: list[ImportReceipt]


class ResourceList(BaseModel):
    items: list[ResourceDetail]
    total: int
    limit: int
    offset: int


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)


class ResourcePatch(RevisionRequest):
    title: str | None = None
    tags: list[str] | None = None
    user_text: str | None = None
    caption: str | None = None
    ocr_text: str | None = None
    workflow: Workflow | None = None


class SearchItemResponse(BaseModel):
    resource_id: UUID
    input_kind: InputKind
    title: str
    tags: list[str]
    summary: str
    score: float
    detail_url: str
    original_url: str | None


class SearchResponse(BaseModel):
    mode: Literal["vector"]
    items: list[SearchItemResponse]


class OriginalResponse(StreamingResponse):
    """Always close the object, including disconnects and streaming failures."""
    def __init__(self, stream, media_type):
        self.stream = stream
        super().__init__(iter(lambda: stream.read(64 * 1024), b""), media_type=media_type,
                         headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.stream.close()


async def _no_store(response: Response) -> None:
    """Prevent browsers and proxies from replaying a stale API snapshot."""
    response.headers["Cache-Control"] = "no-store"


def create_app(config: AppConfig | None = None, sessions=None, storage_resolver=None, search_service=None,
               health_service=None) -> FastAPI:
    config = config or load_app_config()
    owned_sessions = sessions is None
    owned_resolver = storage_resolver is None
    owned_health = health_service is None
    sessions = sessions if sessions is not None else create_session_factory(config.database_url)
    resolver = storage_resolver if storage_resolver is not None else StorageResolver(config)
    owned_search = search_service is None
    search_service = search_service if search_service is not None else SearchService(sessions, config)
    # The health snapshot is taken once per process, from the settings this app actually serves with;
    # probes run in a child the lifespan owns. Model services still come from the environment.
    health_service = (health_service if health_service is not None
                      else HealthService(health_config_from_app(config), runner=SubprocessProbeRunner()))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                try:
                    try:
                        if owned_health:
                            # Cancels the round and reaps the probe child before releasing resources.
                            await health_service.aclose()
                    finally:
                        if owned_search:
                            search_service.close()
                finally:
                    if owned_resolver:
                        resolver.close()
            finally:
                if owned_sessions:
                    sessions.kw["bind"].dispose()

    app = FastAPI(title="MMKB Resource API", version="0.1.0", lifespan=lifespan)
    service = ResourceService(sessions, config, resolver)
    imports = ImportService(service)
    app.state.resources = service
    app.state.imports = imports
    app.state.search = search_service
    app.state.health = health_service

    @app.exception_handler(ResourceError)
    async def resource_error(request: Request, exc: ResourceError):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Omit rejected input values, which may contain private text or files.
        return JSONResponse(status_code=422, content={
            "code": "validation_error", "message": "Invalid request fields",
            "details": [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()],
        })

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"code": "http_error", "message": str(exc.detail)})

    @app.exception_handler(sa.exc.SQLAlchemyError)
    async def database_error(request: Request, exc: sa.exc.SQLAlchemyError):
        logger.error("Database operation failed error_type=%s", type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "code": "database_unavailable", "message": "Database unavailable; check connection and migrations",
        })

    # response_model stays None so the service payload is returned verbatim while the response
    # model still documents the contract in OpenAPI.
    @app.get("/api/health", response_model=None, dependencies=[Depends(_no_store)],
             responses={200: {"model": HealthReport}})
    async def health_status():
        # A completed report always answers 200, including dependency failures; bugs still raise.
        return await health_service.report()

    @app.get("/api/imports/options", response_model=ImportOptions, dependencies=[Depends(_no_store)])
    def import_options():
        return imports.options()

    @app.post("/api/imports/resolve", response_model=ImportResolution, dependencies=[Depends(_no_store)])
    def resolve_imports(body: ImportResolveRequest):
        return imports.resolve(body.keys)

    @app.post("/api/resources", status_code=201, response_model=CreatedResource, response_model_exclude_unset=True)
    def create_resource(
        response: Response,
        file: Annotated[UploadFile | None, File()] = None,
        title: Annotated[str, Form()] = "",
        tags: Annotated[list[str] | None, Form()] = None,
        user_text: Annotated[str | None, Form()] = None,
        workflow: Annotated[Workflow | None, Form()] = None,
        idempotency_key: Annotated[UUID | None, Header()] = None,
        duplicate_policy: Annotated[Literal["skip", "create"], Form()] = "create",
        expected_sha256: Annotated[str | None, Form(pattern="^[0-9a-f]{64}$")] = None,
    ):
        try:
            fields = dict(source=file.file if file else None, filename=file.filename if file else None,
                          title=title, tags=tags, user_text=user_text, workflow=workflow)
            if idempotency_key is None:
                if duplicate_policy != "create" or expected_sha256 is not None:
                    raise ResourceError("import_key_required", "Use Idempotency-Key with import options")
                return service.create(**fields)
            result = imports.create(idempotency_key, duplicate_policy=duplicate_policy,
                                    expected_sha256=expected_sha256, **fields)
            if result["replayed"] or result["import_outcome"] == "skipped":
                response.status_code = 200
            return result
        finally:
            if file:
                file.file.close()

    @app.post("/api/search", response_model=SearchResponse)
    def search_resources(
        file: Annotated[UploadFile | None, File()] = None,
        text: Annotated[str | None, Form()] = None,
        tags: Annotated[list[str] | None, Form()] = None,
        top_k: Annotated[int, Form(ge=1, le=100)] = 20,
    ):
        try:
            return search_service.search(text=text, source=file.file if file else None,
                                         filename=file.filename if file else None, tags=tags, top_k=top_k)
        finally:
            if file:
                file.file.close()

    @app.get("/api/resources", response_model=ResourceList)
    def list_resources(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Status | None = None,
        input_kind: InputKind | None = None,
        tags: Annotated[list[str] | None, Query()] = None,
    ):
        return service.list(limit=limit, offset=offset, status=status, input_kind=input_kind, tags=tags)

    @app.get("/api/resources/{resource_id}", response_model=ResourceDetail)
    def get_resource(resource_id: UUID):
        return service.get(resource_id)

    @app.patch("/api/resources/{resource_id}", response_model=ResourceDetail)
    def update_resource(resource_id: UUID, patch: ResourcePatch):
        changes = patch.model_dump(exclude_unset=True, exclude={"revision"})
        return service.update(resource_id, patch.revision, changes)

    @app.post("/api/resources/{resource_id}/retry", response_model=ResourceDetail)
    def retry_resource(resource_id: UUID, body: RevisionRequest):
        return service.retry(resource_id, body.revision)

    @app.delete("/api/resources/{resource_id}", status_code=204)
    def delete_resource(resource_id: UUID):
        service.delete(resource_id)
        return Response(status_code=204)

    @app.get("/api/resources/{resource_id}/original", response_class=StreamingResponse)
    def original_resource(resource_id: UUID):
        stream, mime = service.original(resource_id)
        return OriginalResponse(stream, mime)

    return app
