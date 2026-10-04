"""Tests for main.py's per-clip audio extraction wiring."""

import os
import types
from unittest.mock import MagicMock, patch

import main


def _args(**overrides):
    base = dict(video=os.path.join("some", "dir", "2026-10-03 scrim.mkv"))
    base.update(overrides)
    return types.SimpleNamespace(**base)


class TestAudioOutputDir:
    def test_named_after_video_without_extension(self):
        assert main.audio_output_dir(_args(), "out") == os.path.join("out", "2026-10-03 scrim")


class TestMaybeExtractAudio:
    def test_disabled_by_default(self):
        with patch("main.create_audio_clips") as mock_create:
            main._maybe_extract_audio(_args(), [(0, 10)], "out", MagicMock())
        mock_create.assert_not_called()

    def test_disabled_explicitly(self):
        with patch("main.create_audio_clips") as mock_create:
            main._maybe_extract_audio(_args(extract_audio=False), [(0, 10)], "out", MagicMock())
        mock_create.assert_not_called()

    def test_enabled_extracts_into_video_named_folder(self):
        args = _args(extract_audio=True)
        pairs = [(0, 10), (20, 30)]
        with patch("main.create_audio_clips") as mock_create:
            main._maybe_extract_audio(args, pairs, "out", MagicMock())
        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert call_args[0] == (args.video, pairs, os.path.join("out", "2026-10-03 scrim"))

    def test_failure_is_non_fatal_and_removes_empty_folder(self, tmp_path):
        audio_dir = tmp_path / "2026-10-03 scrim"

        def _fail(_video, _pairs, out_dir, **_kw):
            os.makedirs(out_dir)
            raise RuntimeError("ffmpeg failed extracting audio: no stream")

        with patch("main.create_audio_clips", side_effect=_fail):
            main._maybe_extract_audio(_args(extract_audio=True), [(0, 10)], str(tmp_path), MagicMock())
        assert not audio_dir.exists()

    def test_cancel_still_propagates(self):
        import pytest
        with patch("main.create_audio_clips", side_effect=RuntimeError("__cancelled__")):
            with pytest.raises(RuntimeError, match="__cancelled__"):
                main._maybe_extract_audio(_args(extract_audio=True), [(0, 10)], "out", MagicMock())
