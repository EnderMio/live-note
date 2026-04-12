from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket

from live_note.config import AppConfig

from .live_gateway import RemoteLiveGateway
from .session_views import RemoteSessionViews
from .task_commands import RemoteTaskCommands


class LiveGateway(Protocol):
    async def handle(self, websocket: WebSocket) -> None: ...


class SessionViews(Protocol):
    def health_payload(self) -> dict[str, object]: ...
    def list_sessions_payload(self) -> list[dict[str, object]]: ...
    def session_payload(self, session_id: str) -> dict[str, object]: ...
    def artifacts_payload(self, session_id: str) -> dict[str, object]: ...


class TaskCommands(Protocol):
    def request_refine(self, session_id: str, *, request_id: str | None = None) -> dict[str, object]: ...
    def request_republish(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]: ...
    def request_retranscribe(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]: ...
    def request_finalize(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]: ...
    def create_import_task(
        self,
        *,
        filename: str,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
        request_id: str | None,
        file_bytes: bytes,
    ) -> dict[str, object]: ...
    def import_task_payload(self, task_id: str) -> dict[str, object]: ...
    def cancel_import_task(self, task_id: str) -> dict[str, object]: ...
    def list_tasks_payload(self) -> dict[str, object]: ...
    def task_payload(self, task_id: str) -> dict[str, object]: ...
    def cancel_task(self, task_id: str) -> dict[str, object]: ...


def create_remote_app(
    views: SessionViews,
    commands: TaskCommands,
    *,
    api_token: str | None = None,
    live_gateway: LiveGateway | None = None,
    lifespan=None,
) -> FastAPI:
    app = FastAPI(title="live-note-remote", lifespan=lifespan)

    @app.get("/api/v1/health")
    def health(authorization: str | None = Header(default=None)) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        return views.health_payload()

    @app.get("/api/v1/sessions")
    def sessions(authorization: str | None = Header(default=None)) -> list[dict[str, object]]:
        _authorize_http(api_token, authorization)
        return views.list_sessions_payload()

    @app.get("/api/v1/sessions/{session_id}")
    def session(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return views.session_payload(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/artifacts")
    def artifacts(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return views.artifacts_payload(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/refine")
    def refine(
        session_id: str,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.request_refine(session_id, request_id=request_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/retranscribe")
    def retranscribe(
        session_id: str,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.request_retranscribe(session_id, request_id=request_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/republish")
    def republish(
        session_id: str,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.request_republish(session_id, request_id=request_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/finalize")
    def finalize(
        session_id: str,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.request_finalize(session_id, request_id=request_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/imports")
    async def create_import(
        request: Request,
        filename: str,
        title: str | None = None,
        kind: str = "generic",
        language: str | None = None,
        speaker_enabled: bool | None = None,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        body = await request.body()
        try:
            return commands.create_import_task(
                filename=filename,
                title=title,
                kind=kind,
                language=language,
                speaker_enabled=speaker_enabled,
                request_id=request_id,
                file_bytes=body,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/imports/{task_id}")
    def import_status(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.import_task_payload(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/imports/{task_id}/actions/cancel")
    def cancel_import(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.cancel_import_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/tasks")
    def tasks(authorization: str | None = Header(default=None)) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        return commands.list_tasks_payload()

    @app.get("/api/v1/tasks/{task_id}")
    def task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.task_payload(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/tasks/{task_id}/actions/cancel")
    def cancel_task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(api_token, authorization)
        try:
            return commands.cancel_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.websocket("/api/v1/live")
    async def live(websocket: WebSocket) -> None:
        if not _authorized_token(api_token, websocket.headers.get("authorization")):
            await websocket.close(code=4401, reason="unauthorized")
            return
        if live_gateway is None:
            await websocket.close(code=1011, reason="live gateway unavailable")
            return
        await live_gateway.handle(websocket)

    return app


def build_remote_app(config: AppConfig) -> FastAPI:
    commands = RemoteTaskCommands(config)
    views = RemoteSessionViews(config, server_id=commands.server_id)
    gateway = RemoteLiveGateway(
        config,
        commit_postprocess_handoff=commands.commit_postprocess_handoff,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        commands.start()
        try:
            yield
        finally:
            commands.shutdown()

    app = create_remote_app(
        views,
        commands,
        api_token=config.serve.api_token or config.remote.api_token,
        live_gateway=gateway,
        lifespan=lifespan,
    )
    return app


def _authorize_http(expected_token: str | None, authorization: str | None) -> None:
    if not _authorized_token(expected_token, authorization):
        raise HTTPException(status_code=401, detail="unauthorized")


def _authorized_token(expected_token: str | None, authorization: str | None) -> bool:
    if not expected_token:
        return True
    if not authorization:
        return False
    return authorization.strip() == f"Bearer {expected_token}"
