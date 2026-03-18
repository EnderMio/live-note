from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from live_note.remote.api import create_remote_app


class _FakeRemoteService:
    def health_payload(self) -> dict[str, object]:
        return {"status": "ok", "service": "live-note-remote"}

    def list_sessions_payload(self) -> list[dict[str, object]]:
        return [{"session_id": "session-1", "title": "机器学习导论"}]

    def session_payload(self, session_id: str) -> dict[str, object]:
        return {"session_id": session_id, "status": "finalized"}

    def artifacts_payload(self, session_id: str) -> dict[str, object]:
        return {
            "session_id": session_id,
            "metadata": {
                "session_id": session_id,
                "title": "机器学习导论",
                "kind": "lecture",
            },
            "entries": [
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 1200,
                    "text": "今天讲梯度下降。",
                    "speaker_label": "Speaker 1",
                }
            ],
        }

    def request_refine(self, session_id: str) -> dict[str, object]:
        return {"session_id": session_id, "accepted": True}

    async def live_session(self, websocket) -> None:
        await websocket.accept()
        payload = await websocket.receive_json()
        if payload["type"] != "start":
            raise AssertionError("expected start event")
        await websocket.send_json(
            {
                "type": "session_started",
                "session_id": "session-1",
                "status": "listening",
            }
        )
        await websocket.receive_bytes()
        stop_payload = await websocket.receive_json()
        if stop_payload["type"] != "stop":
            raise AssertionError("expected stop event")
        await websocket.send_json(
            {
                "type": "completed",
                "session_id": "session-1",
                "status": "done",
            }
        )
        await websocket.close()


class RemoteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_remote_app(_FakeRemoteService()))

    def test_health_endpoint_returns_service_payload(self) -> None:
        response = self.client.get("/api/v1/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"status": "ok", "service": "live-note-remote"},
            response.json(),
        )

    def test_artifacts_endpoint_returns_entries_with_speaker_labels(self) -> None:
        response = self.client.get("/api/v1/sessions/session-1/artifacts")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("session-1", payload["session_id"])
        self.assertEqual("Speaker 1", payload["entries"][0]["speaker_label"])

    def test_live_websocket_emits_start_and_complete_events(self) -> None:
        with self.client.websocket_connect("/api/v1/live") as websocket:
            websocket.send_json({"type": "start", "title": "机器学习导论"})
            started = websocket.receive_json()
            websocket.send_bytes(b"\x00\x00" * 320)
            websocket.send_json({"type": "stop"})
            completed = websocket.receive_json()

        self.assertEqual("session_started", started["type"])
        self.assertEqual("completed", completed["type"])
