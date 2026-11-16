"""FastAPI resource API. Run: uvicorn mmkb.api:create_app --factory"""
from contextlib import asynccontextmanager
from datetime import datetime
import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa
from starlette.exceptions import HTTPException

from mmkb.config import AppConfig, load_app_config
from mmkb.db.session import create_session_factory
from mmkb.errors import ResourceError
from mmkb.resources import ResourceService
from mmkb.storage import StorageResolver

logger = logging.getLogger(__name__)
Workflow = Literal["structured_image", "visual_image", "visdoc", "text_image", "text"]
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


def create_app(config: AppConfig | None = None, sessions=None, storage_resolver=None) -> FastAPI:
    config = config or load_app_config()
    owned_sessions = sessions is None
    owned_resolver = storage_resolver is None
    sessions = sessions if sessions is not None else create_session_factory(config.database_url)
    resolver = storage_resolver if storage_resolver is not None else StorageResolver(config)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                if owned_resolver:
                    resolver.close()
            finally:
                if owned_sessions:
                    sessions.kw["bind"].dispose()

    app = FastAPI(title="MMKB Resource API", version="0.1.0", lifespan=lifespan)
    service = ResourceService(sessions, config, resolver)
    app.state.resources = service

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

    @app.post("/api/resources", status_code=201, response_model=CreatedResource)
    def create_resource(
        file: Annotated[UploadFile | None, File()] = None,
        title: Annotated[str, Form()] = "",
        tags: Annotated[list[str] | None, Form()] = None,
        user_text: Annotated[str | None, Form()] = None,
        workflow: Annotated[Workflow | None, Form()] = None,
    ):
        try:
            return service.create(source=file.file if file else None, filename=file.filename if file else None,
                                  title=title, tags=tags, user_text=user_text, workflow=workflow)
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
