from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, WebSocket

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
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_http(service, authorization)
        try:
            return service.request_refine(session_id)
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
