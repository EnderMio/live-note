from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from live_note.remote.api import create_remote_app


class _FakeRemoteService:
    def __init__(self) -> None:
        self.import_request: dict[str, object] | None = None

    def health_payload(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "live-note-remote",
            "supports_imports": True,
            "supports_tasks": True,
            "server_id": "server-1",
        }

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
            "transcript_content": "# 原文\n",
            "structured_content": "# 整理\n",
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
        return {
            "task_id": "task-refine-1",
            "server_id": "server-1",
            "action": "refine",
            "session_id": session_id,
            "status": "queued",
            "stage": "queued",
            "message": "已加入远端队列。",
        }

    def request_retranscribe(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "task_id": "task-retranscribe-1",
            "server_id": "server-1",
            "action": "retranscribe",
            "session_id": session_id,
            "status": "queued",
            "stage": "queued",
            "message": "已加入远端队列。",
        }

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
    ) -> dict[str, object]:
        self.import_request = {
            "filename": filename,
            "title": title,
            "kind": kind,
            "language": language,
            "speaker_enabled": speaker_enabled,
            "request_id": request_id,
            "file_bytes": file_bytes,
        }
        return {
            "task_id": "import-1",
            "server_id": "server-1",
            "action": "import",
            "status": "queued",
            "message": "已接收上传。",
        }

    def import_task_payload(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "status": "running",
            "stage": "transcribing",
            "message": "正在转写片段 1/3",
            "current": 1,
            "total": 3,
        }

    def cancel_import_task(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "status": "cancelled",
            "stage": "cancelled",
            "message": "远端导入已取消。",
        }

    def list_tasks_payload(self) -> dict[str, object]:
        return {
            "server_id": "server-1",
            "active": [
                {
                    "task_id": "task-1",
                    "server_id": "server-1",
                    "action": "import",
                    "label": "文件导入",
                    "status": "running",
                    "stage": "transcribing",
                    "message": "正在转写片段 1/3",
                    "session_id": "session-1",
                    "request_id": "req-1",
                    "result_version": 1,
                    "can_cancel": True,
                }
            ],
            "recent": [],
        }

    def task_payload(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "server_id": "server-1",
            "action": "import",
            "label": "文件导入",
            "status": "running",
            "stage": "transcribing",
            "message": "正在转写片段 1/3",
            "session_id": "session-1",
            "request_id": "req-1",
            "result_version": 1,
            "can_cancel": True,
        }

    def cancel_task(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "server_id": "server-1",
            "action": "import",
            "status": "cancelled",
            "stage": "cancelled",
            "message": "已取消。",
        }

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
                "type": "stop_received",
                "session_id": "session-1",
                "message": "远端已确认停止，后台整理任务已创建。",
                "postprocess_task": {
                    "task_id": "task-post-1",
                    "server_id": "server-1",
                    "action": "postprocess",
                    "label": "后台整理",
                    "session_id": "session-1",
                    "status": "running",
                    "stage": "handoff",
                    "message": "后台整理已接管。",
                    "result_version": 0,
                    "can_cancel": False,
                },
            }
        )
        await websocket.send_json(
            {
                "type": "completed",
                "session_id": "session-1",
            }
        )
        await websocket.close()


class RemoteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _FakeRemoteService()
        self.client = TestClient(create_remote_app(self.service))

    def test_health_endpoint_returns_service_payload(self) -> None:
        response = self.client.get("/api/v1/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "status": "ok",
                "service": "live-note-remote",
                "supports_imports": True,
                "supports_tasks": True,
                "server_id": "server-1",
            },
            response.json(),
        )

    def test_artifacts_endpoint_returns_entries_with_speaker_labels(self) -> None:
        response = self.client.get("/api/v1/sessions/session-1/artifacts")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("session-1", payload["session_id"])
        self.assertEqual("Speaker 1", payload["entries"][0]["speaker_label"])
        self.assertEqual("# 原文\n", payload["transcript_content"])
        self.assertEqual("# 整理\n", payload["structured_content"])

    def test_live_websocket_emits_stop_ack_before_completed(self) -> None:
        with self.client.websocket_connect("/api/v1/live") as websocket:
            websocket.send_json({"type": "start", "title": "机器学习导论"})
            started = websocket.receive_json()
            websocket.send_bytes(b"\x00\x00" * 320)
            websocket.send_json({"type": "stop"})
            stop_received = websocket.receive_json()
            completed = websocket.receive_json()

        self.assertEqual("session_started", started["type"])
        self.assertEqual("stop_received", stop_received["type"])
        self.assertEqual("task-post-1", stop_received["postprocess_task"]["task_id"])
        self.assertEqual("completed", completed["type"])
        self.assertNotIn("postprocess_task", completed)

    def test_create_import_endpoint_passes_uploaded_audio_to_service(self) -> None:
        response = self.client.post(
            "/api/v1/imports",
            params={
                "filename": "第1课.mp3",
                "title": "股票课",
                "kind": "lecture",
                "language": "zh",
                "speaker_enabled": "1",
            },
            content=b"fake-audio",
            headers={"Content-Type": "application/octet-stream"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("import-1", response.json()["task_id"])
        self.assertEqual(
            {
                "filename": "第1课.mp3",
                "title": "股票课",
                "kind": "lecture",
                "language": "zh",
                "speaker_enabled": True,
                "request_id": None,
                "file_bytes": b"fake-audio",
            },
            self.service.import_request,
        )

    def test_get_import_task_endpoint_returns_status_payload(self) -> None:
        response = self.client.get("/api/v1/imports/import-1")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("running", payload["status"])
        self.assertEqual("transcribing", payload["stage"])

    def test_cancel_import_task_endpoint_returns_cancelled_payload(self) -> None:
        response = self.client.post("/api/v1/imports/import-1/actions/cancel")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("cancelled", payload["status"])
        self.assertEqual("cancelled", payload["stage"])

    def test_list_tasks_endpoint_returns_active_and_recent_tasks(self) -> None:
        response = self.client.get("/api/v1/tasks")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("server-1", payload["server_id"])
        self.assertEqual("task-1", payload["active"][0]["task_id"])

    def test_get_task_endpoint_returns_task_payload(self) -> None:
        response = self.client.get("/api/v1/tasks/task-1")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("task-1", payload["task_id"])
        self.assertEqual("import", payload["action"])

    def test_cancel_task_endpoint_returns_task_payload(self) -> None:
        response = self.client.post("/api/v1/tasks/task-1/actions/cancel")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("cancelled", payload["status"])

    def test_retranscribe_endpoint_returns_task_payload(self) -> None:
        response = self.client.post("/api/v1/sessions/session-1/actions/retranscribe")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("task-retranscribe-1", payload["task_id"])
        self.assertEqual("retranscribe", payload["action"])
