"""
test_match_label.py — Unit tests for the "Match N" label option in main.py.

Pure-function tests; no video/tesseract/ffmpeg dependencies.
"""

import sys
import types

import main
from main import _label_kwargs, chapter_titles, write_chapter_timestamps


class TestChapterTitles:
    def test_titles(self):
        assert chapter_titles(3) == ["Match 1", "Match 2", "Match 3"]

    def test_zero(self):
        assert chapter_titles(0) == []

    def test_chapters_file_uses_titles(self, tmp_path):
        out = tmp_path / "final_output.mp4"
        path = write_chapter_timestamps([(0, 30), (40, 100)], str(out))
        assert open(path).read() == "0:00 Match 1\n0:30 Match 2\n"


class TestLabelKwargs:
    def test_off_by_default(self):
        assert _label_kwargs(types.SimpleNamespace(), 2) == {}

    def test_on_uses_chapter_titles_and_pos(self):
        args = types.SimpleNamespace(match_label=True, match_label_pos=[0.1, 0.9])
        assert _label_kwargs(args, 2) == {
            "labels": ["Match 1", "Match 2"],
            "label_pos": (0.1, 0.9),
        }

    def test_on_without_pos_uses_default(self):
        args = types.SimpleNamespace(match_label=True)
        assert _label_kwargs(args, 1)["label_pos"] == (0.5, 0.05)


class TestParseArgs:
    def _parse(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["main.py", "in.mp4", *argv])
        return main.parse_args()

    def test_defaults(self, monkeypatch):
        args = self._parse(monkeypatch)
        assert args.match_label is False
        assert args.match_label_pos == [0.5, 0.05]

    def test_flags(self, monkeypatch):
        args = self._parse(monkeypatch, "--match-label", "--match-label-pos", "0.2", "0.8")
        assert args.match_label is True
        assert args.match_label_pos == [0.2, 0.8]
