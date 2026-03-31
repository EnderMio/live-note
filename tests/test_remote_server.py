from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.app.journal import SessionWorkspace
from live_note.domain import SessionMetadata
from live_note.remote.server import build_session_artifacts_payload, serve_remote_app


class RemoteServerTests(unittest.TestCase):
    def test_build_session_artifacts_payload_includes_entries_and_session_audio_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_root = root / "session-1"
            workspace = SessionWorkspace.create(
                session_root,
                _sample_metadata(session_root),
            )
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"wav")
            workspace.record_segment_created("seg-00001", 0, 2000, wav_path)
            workspace.record_segment_text(
                "seg-00001",
                0,
                2000,
                "大家好。",
                speaker_label="Speaker 1",
            )
            workspace.session_live_wav.write_bytes(b"RIFF")
            workspace.write_transcript("# 原文\n")
            workspace.write_structured("# 整理\n")

            payload = build_session_artifacts_payload(workspace)

        self.assertEqual("session-1", payload["metadata"]["session_id"])
        self.assertTrue(payload["has_session_audio"])
        self.assertEqual("Speaker 1", payload["entries"][0]["speaker_label"])
        self.assertEqual("# 原文\n", payload["transcript_content"])
        self.assertEqual("# 整理\n", payload["structured_content"])

    def test_serve_remote_app_passes_explicit_websocket_ping_settings_to_uvicorn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[audio]",
                        "",
                        "[import]",
                        "",
                        "[refine]",
                        "",
                        "[whisper]",
                        'binary = "/Users/demo/whisper-server"',
                        'model = "./model.bin"',
                        "",
                        "[obsidian]",
                        "enabled = false",
                        'base_url = "https://127.0.0.1:27124"',
                        'transcript_dir = "Sessions/Transcripts"',
                        'structured_dir = "Sessions/Summaries"',
                        "",
                        "[llm]",
                        "enabled = false",
                        'base_url = "https://api.openai.com/v1"',
                        'model = "gpt-4.1-mini"',
                        "",
                        "[serve]",
                        'host = "0.0.0.0"',
                        "port = 18765",
                        'api_token = "server-token"',
                        "ws_ping_interval_seconds = 33",
                        "ws_ping_timeout_seconds = 47",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "model.bin").write_bytes(b"model")

            fake_uvicorn = types.ModuleType("uvicorn")

            with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
                with patch.object(fake_uvicorn, "run", create=True) as run_mock:
                    exit_code = serve_remote_app(config_path)

        self.assertEqual(0, exit_code)
        self.assertEqual(33, run_mock.call_args.kwargs["ws_ping_interval"])
        self.assertEqual(47, run_mock.call_args.kwargs["ws_ping_timeout"])


def _sample_metadata(session_dir: Path) -> SessionMetadata:
    return SessionMetadata(
        session_id="session-1",
        title="产品周会",
        kind="meeting",
        input_mode="live",
        source_label="BlackHole 2ch",
        source_ref="1",
        language="zh",
        started_at="2026-03-18T10:00:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-18/demo.md",
        structured_note_path="Sessions/Summaries/2026-03-18/demo.md",
        session_dir=str(session_dir),
        status="finalized",
        transcript_source="refined",
        refine_status="done",
        execution_target="remote",
        remote_session_id="session-1",
        speaker_status="done",
    )
