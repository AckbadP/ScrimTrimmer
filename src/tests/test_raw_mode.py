"""
test_raw_mode.py — Raw mode: detect matches, keep the source video uncut,
and emit chapters at source-video timestamps.
"""

import os
import types

import pytest

import main as main_module
from main import raw_chapter_lines


class TestRawChapterLines:
    def test_match_at_start_is_labelled_at_zero(self):
        assert raw_chapter_lines([(0, 100)], 100) == ["0:00 Match 1"]

    def test_short_lead_in_folds_into_first_match(self):
        assert raw_chapter_lines([(5, 100)], 100) == ["0:00 Match 1"]

    def test_pre_match_chapter(self):
        assert raw_chapter_lines([(30, 100)], 100) == ["0:00 Pre-match", "0:30 Match 1"]

    def test_break_between_matches(self):
        assert raw_chapter_lines([(0, 100), (160, 300)], 300) == [
            "0:00 Match 1",
            "1:40 Break",
            "2:40 Match 2",
        ]

    def test_short_gap_folds_into_previous_match(self):
        assert raw_chapter_lines([(0, 100), (105, 300)], 300) == [
            "0:00 Match 1",
            "1:45 Match 2",
        ]

    def test_post_match_tail(self):
        assert raw_chapter_lines([(0, 100)], 200) == ["0:00 Match 1", "1:40 Post-match"]

    def test_short_tail_omitted(self):
        assert raw_chapter_lines([(0, 100)], 105) == ["0:00 Match 1"]

    def test_hour_long_video(self):
        assert raw_chapter_lines([(3600, 3700)], 3700) == ["0:00 Pre-match", "1:00:00 Match 1"]


@pytest.fixture
def raw_run(tmp_path, monkeypatch):
    """Run main.run() in raw chat-log mode with detection and I/O stubbed."""
    video = tmp_path / "source.mkv"
    video.write_bytes(b"")
    out = tmp_path / "out"

    monkeypatch.setattr(main_module, "_check_dependencies", lambda args: None)
    monkeypatch.setattr(main_module, "check_source_video", lambda path: None)
    monkeypatch.setattr(main_module, "get_video_duration", lambda path: 600)
    monkeypatch.setattr(
        main_module, "parse_chat_logs",
        lambda *a, **kw: ([60, 300], [200, 500]),
    )

    def _no_clipping(*a, **kw):
        raise AssertionError("raw mode must not cut the video")

    monkeypatch.setattr(main_module, "create_clips", _no_clipping)
    monkeypatch.setattr(main_module, "stitch_clips", _no_clipping)

    uploads = []

    def _fake_upload(args, video_path, description, notify):
        uploads.append((video_path, description))
        return "https://youtu.be/x"

    monkeypatch.setattr(main_module, "_maybe_upload", _fake_upload)

    args = types.SimpleNamespace(
        video=str(video),
        output=str(out),
        chat_region=[0.0, 0.35, 0.15, 1.0],
        verbose=False,
        chat_logs=[str(tmp_path / "log.txt")],
        t0="12:00:00",
        chapters_dir=None,
        run_without_ffmpeg=False,
        force_ocr=False,
        youtube_title="Scrims",
        raw_mode=True,
        match_label=True,
    )
    result = main_module.run(args)
    return result, uploads, args, out


class TestRawRun:
    _EXPECTED = (
        "0:00 Pre-match\n"
        "1:00 Match 1\n"
        "3:20 Break\n"
        "5:00 Match 2\n"
        "8:20 Post-match\n"
    )

    def test_returns_source_timestamps(self, raw_run):
        (chapters_text, url), _, _, _ = raw_run
        assert chapters_text == self._EXPECTED
        assert url == "https://youtu.be/x"

    def test_uploads_source_video(self, raw_run):
        _, uploads, args, _ = raw_run
        assert uploads == [(args.video, self._EXPECTED)]

    def test_writes_only_chapters_file(self, raw_run):
        _, _, _, out = raw_run
        assert sorted(os.listdir(out)) == ["Scrims_chapters.txt"]
