from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from live_note.config import ObsidianConfig
from live_note.obsidian.client import ObsidianClient


class FakeResponse:
    def __init__(self, body: bytes = b""):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None


class ObsidianClientTests(unittest.TestCase):
    def test_put_note_retries_and_sends_authorization(self) -> None:
        requests = []
        attempts = {"count": 0}

        def fake_urlopen(request, timeout, context):
            attempts["count"] += 1
            requests.append(request)
            if attempts["count"] == 1:
                raise HTTPError(
                    request.full_url,
                    500,
                    "server error",
                    hdrs=None,
                    fp=BytesIO(b"retry"),
                )
            return FakeResponse()

        client = ObsidianClient(
            ObsidianConfig(
                base_url="http://127.0.0.1:27124",
                transcript_dir="Sessions/Transcripts",
                structured_dir="Sessions/Summaries",
                verify_ssl=True,
                timeout_seconds=2,
                retry_attempts=2,
                retry_backoff_seconds=0.01,
                api_key="token",
            )
        )

        with patch("live_note.obsidian.client.urlopen", side_effect=fake_urlopen):
            client.put_note("Sessions/Transcripts/2026-03-15/My Note.md", "hello")

        self.assertEqual(2, attempts["count"])
        self.assertEqual(
            "http://127.0.0.1:27124/vault/Sessions/Transcripts/2026-03-15/My%20Note.md",
            requests[-1].full_url,
        )
        self.assertEqual("Bearer token", requests[-1].headers["Authorization"])
        self.assertEqual(b"hello", requests[-1].data)

    def test_put_note_is_noop_when_obsidian_sync_disabled(self) -> None:
        client = ObsidianClient(
            ObsidianConfig(
                base_url="https://127.0.0.1:27124",
                transcript_dir="Sessions/Transcripts",
                structured_dir="Sessions/Summaries",
                enabled=False,
            )
        )

        with patch("live_note.obsidian.client.urlopen") as urlopen_mock:
            client.put_note("Sessions/Transcripts/demo.md", "hello")

        urlopen_mock.assert_not_called()
