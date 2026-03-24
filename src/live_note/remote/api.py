from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket

from live_note.config import AppConfig

from .service import RemoteSessionService


def create_remote_app(service: RemoteSessionService) -> FastAPI:
    app = FastAPI(title="live-note-remote")

    @app.get("/api/v1/health")
    def health(authorization: str | None = Header(default=None)) -> dict[str, object]:
        _authorize_http(service, authorization)
        return service.health_payload()

    @app.get("/api/v1/sessions")
    def sessions(authorization: str | None = Header(default=None)) -> list[dict[str, object]]:
        _authorize_http(service, authorization)
        return service.list_sessions_payload()

    @app.get("/api/v1/sessions/{session_id}")
    def session(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.session_payload(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/artifacts")
    def artifacts(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.artifacts_payload(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/refine")
    def refine(
        session_id: str,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.request_refine(session_id, request_id=request_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/retranscribe")
    def retranscribe(
        session_id: str,
        request_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.request_retranscribe(session_id, request_id=request_id)
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
        _authorize_http(service, authorization)
        body = await request.body()
        try:
            return service.create_import_task(
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
        _authorize_http(service, authorization)
        try:
            return service.import_task_payload(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/imports/{task_id}/actions/cancel")
    def cancel_import(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.cancel_import_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/tasks")
    def tasks(authorization: str | None = Header(default=None)) -> dict[str, object]:
        _authorize_http(service, authorization)
        return service.list_tasks_payload()

    @app.get("/api/v1/tasks/{task_id}")
    def task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.task_payload(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/tasks/{task_id}/actions/cancel")
    def cancel_task(
        task_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.cancel_task(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.websocket("/api/v1/live")
    async def live(websocket: WebSocket) -> None:
        if not _authorized_token(
            getattr(service, "api_token", None),
            websocket.headers.get("authorization"),
        ):
            await websocket.close(code=4401, reason="unauthorized")
            return
        await service.live_session(websocket)

    return app


def build_remote_app(config: AppConfig) -> FastAPI:
    return create_remote_app(RemoteSessionService(config))


def _authorize_http(service: RemoteSessionService, authorization: str | None) -> None:
    if not _authorized_token(getattr(service, "api_token", None), authorization):
        raise HTTPException(status_code=401, detail="unauthorized")


def _authorized_token(expected_token: str | None, authorization: str | None) -> bool:
    if not expected_token:
        return True
    if not authorization:
        return False
    return authorization.strip() == f"Bearer {expected_token}"
