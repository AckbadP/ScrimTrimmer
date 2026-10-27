"""
Tests for video_clipper.py — extract_clip, stitch_clips, create_clips.

subprocess.run is mocked so no real ffmpeg or video files are needed.
Cancellation tests mock subprocess.Popen instead (the cancel-aware code path
never calls subprocess.run), except for one real-process test that verifies
the actual terminate/kill mechanics.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

import video_clipper
from video_clipper import create_clips, extract_clip, stitch_clips

# Capture the real function before any monkeypatching by fixtures.
_real_use_python_clipper = video_clipper._use_python_clipper


def _ok():
    m = MagicMock()
    m.returncode = 0
    return m


def _fail(stderr="ffmpeg error"):
    m = MagicMock()
    m.returncode = 1
    m.stderr = stderr
    return m


# Force the ffmpeg code path for all tests in this module.
@pytest.fixture(autouse=True)
def force_ffmpeg(monkeypatch):
    import video_clipper
    monkeypatch.setattr(video_clipper, "_force_python", False)
    monkeypatch.setattr(video_clipper, "_use_python_clipper", lambda: False)


# ---------------------------------------------------------------------------
# stitch_clips
# ---------------------------------------------------------------------------

class TestStitchClips:
    def test_empty_list_raises_value_error(self):
        with pytest.raises(ValueError, match="No clips"):
            stitch_clips([], "out.mp4")

    def test_ffmpeg_failure_raises_runtime_error(self):
        with patch("subprocess.run", return_value=_fail("concat error")):
            with pytest.raises(RuntimeError, match="ffmpeg concat"):
                stitch_clips(["a.mp4"], "out.mp4")

    def test_command_uses_concat_demuxer(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            stitch_clips(["a.mp4"], "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-f" in cmd
        assert "concat" in cmd

    def test_command_uses_stream_copy(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            stitch_clips(["a.mp4"], "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd
        assert "copy" in cmd

    def test_output_path_in_command(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            stitch_clips(["a.mp4"], "final.mp4")
        cmd = mock_run.call_args[0][0]
        assert "final.mp4" in cmd

    def test_concat_list_file_deleted_on_success(self):
        # The temp concat list file must be cleaned up after a successful run.
        created_files = []
        original_unlink = os.unlink

        def tracking_unlink(path):
            created_files.append(path)
            original_unlink(path)

        with patch("subprocess.run", return_value=_ok()):
            with patch("os.unlink", side_effect=tracking_unlink):
                stitch_clips(["a.mp4"], "out.mp4")

        assert len(created_files) == 1
        assert not os.path.exists(created_files[0])

    def test_concat_list_file_deleted_on_failure(self):
        # Temp file must be cleaned up even when ffmpeg fails.
        created_files = []
        original_unlink = os.unlink

        def tracking_unlink(path):
            created_files.append(path)
            original_unlink(path)

        with patch("subprocess.run", return_value=_fail()):
            with patch("os.unlink", side_effect=tracking_unlink):
                with pytest.raises(RuntimeError):
                    stitch_clips(["a.mp4"], "out.mp4")

        assert len(created_files) == 1


# ---------------------------------------------------------------------------
# extract_clip
# ---------------------------------------------------------------------------

class TestExtractClip:
    def test_ffmpeg_failure_raises_runtime_error(self):
        with patch("subprocess.run", return_value=_fail("bad input")):
            with pytest.raises(RuntimeError, match="ffmpeg failed"):
                extract_clip("in.mp4", 0, 10, "out.mp4")

    def test_start_and_end_times_in_command(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("in.mp4", 15, 45, "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "15" in cmd and "45" in cmd

    def test_input_and_output_paths_in_command(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("source.mkv", 0, 10, "clip.mp4")
        cmd = mock_run.call_args[0][0]
        assert "source.mkv" in cmd
        assert "clip.mp4" in cmd

    def test_uses_stream_copy(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("in.mp4", 0, 10, "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd
        assert "copy" in cmd

    def test_overwrite_flag_present(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("in.mp4", 0, 10, "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-y" in cmd


# ---------------------------------------------------------------------------
# create_clips
# ---------------------------------------------------------------------------

class TestCreateClips:
    def test_empty_pairs_returns_empty_list(self):
        with patch("subprocess.run", return_value=_ok()):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = create_clips("in.mp4", [], tmpdir)
        assert result == []

    def test_returns_correct_number_of_paths(self):
        with patch("subprocess.run", return_value=_ok()):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = create_clips("in.mp4", [(0, 10), (20, 30), (40, 50)], tmpdir)
        assert len(result) == 3

    def test_clip_paths_are_sequentially_numbered(self):
        with patch("subprocess.run", return_value=_ok()):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = create_clips("in.mp4", [(0, 10), (20, 30)], tmpdir)
        assert os.path.basename(result[0]) == "clip_001.mp4"
        assert os.path.basename(result[1]) == "clip_002.mp4"

    def test_creates_output_directory(self):
        with patch("subprocess.run", return_value=_ok()):
            with tempfile.TemporaryDirectory() as tmpdir:
                new_dir = os.path.join(tmpdir, "clips")
                create_clips("in.mp4", [(0, 10)], new_dir)
                assert os.path.isdir(new_dir)

    def test_clips_in_correct_output_directory(self):
        with patch("subprocess.run", return_value=_ok()):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = create_clips("in.mp4", [(0, 10)], tmpdir)
        assert os.path.dirname(result[0]) == tmpdir


# ---------------------------------------------------------------------------
# Python fallback (no ffmpeg)
# ---------------------------------------------------------------------------

class TestPythonFallback:
    """Tests for the OpenCV fallback used when ffmpeg is not available."""

    @pytest.fixture(autouse=True)
    def no_ffmpeg(self, monkeypatch):
        import video_clipper
        monkeypatch.setattr(video_clipper, "_use_python_clipper", lambda: True)

    def _make_fake_cap(self, width=320, height=240, fps=30.0, frames=10):
        """Return a mock cv2.VideoCapture that yields `frames` black frames."""
        frame = MagicMock()
        reads = [(True, frame)] * frames + [(False, None)]
        cap = MagicMock()
        cap.get.side_effect = lambda prop: {
            0: fps,    # CAP_PROP_FPS
            3: width,  # CAP_PROP_FRAME_WIDTH
            4: height, # CAP_PROP_FRAME_HEIGHT
        }.get(prop, 0)
        cap.read.side_effect = reads
        return cap

    def test_extract_clip_uses_cv2_not_subprocess(self):
        mock_cap = self._make_fake_cap(frames=5)
        mock_writer = MagicMock()
        with patch("subprocess.run") as mock_sub, \
             patch("cv2.VideoCapture", return_value=mock_cap), \
             patch("cv2.VideoWriter", return_value=mock_writer), \
             patch("cv2.VideoWriter_fourcc", return_value=0):
            extract_clip("in.mp4", 0, 5, "out.mp4")
        mock_sub.assert_not_called()
        mock_writer.write.assert_called()

    def test_stitch_clips_uses_cv2_not_subprocess(self):
        # Each VideoCapture() call gets a fresh mock so reads don't run dry.
        mock_writer = MagicMock()
        with patch("subprocess.run") as mock_sub, \
             patch("cv2.VideoCapture", side_effect=lambda _: self._make_fake_cap(frames=3)), \
             patch("cv2.VideoWriter", return_value=mock_writer), \
             patch("cv2.VideoWriter_fourcc", return_value=0):
            stitch_clips(["a.mp4", "b.mp4"], "out.mp4")
        mock_sub.assert_not_called()
        mock_writer.release.assert_called()


# ---------------------------------------------------------------------------
# set_force_python and _use_python_clipper body
# ---------------------------------------------------------------------------

class TestSetForcePython:
    def test_set_force_python_and_use_python_clipper(self, monkeypatch):
        # Restore original function and state after test.
        monkeypatch.setattr(video_clipper, "_use_python_clipper", _real_use_python_clipper)
        monkeypatch.setattr(video_clipper, "_force_python", False)
        video_clipper.set_force_python(True)   # covers line 21
        assert video_clipper._use_python_clipper() is True   # covers line 25 body

    def test_use_python_clipper_when_no_ffmpeg(self, monkeypatch):
        import shutil
        monkeypatch.setattr(video_clipper, "_use_python_clipper", _real_use_python_clipper)
        monkeypatch.setattr(video_clipper, "_force_python", False)
        monkeypatch.setattr(shutil, "which", lambda _: None)
        assert video_clipper._use_python_clipper() is True   # covers line 25 via which()


# ---------------------------------------------------------------------------
# _force_python=True info-print branches (lines 165, 189)
# ---------------------------------------------------------------------------

class TestForcePythonInfoPrint:
    """When _force_python is True the clipper prints an [info] message (not [warning])."""

    def _make_fake_cap(self, frames=5):
        frame = MagicMock()
        reads = [(True, frame)] * frames + [(False, None)]
        cap = MagicMock()
        cap.get.side_effect = lambda prop: {0: 30.0, 3: 320, 4: 240}.get(prop, 0)
        cap.read.side_effect = reads
        return cap

    def test_extract_clip_info_print_when_forced(self, monkeypatch, capsys):
        monkeypatch.setattr(video_clipper, "_force_python", True)
        monkeypatch.setattr(video_clipper, "_use_python_clipper", lambda: True)
        mock_writer = MagicMock()
        with patch("cv2.VideoCapture", return_value=self._make_fake_cap()), \
             patch("cv2.VideoWriter", return_value=mock_writer), \
             patch("cv2.VideoWriter_fourcc", return_value=0):
            extract_clip("in.mp4", 0, 5, "out.mp4")
        out = capsys.readouterr().out
        assert "[info]" in out

    def test_stitch_clips_info_print_when_forced(self, monkeypatch, capsys):
        monkeypatch.setattr(video_clipper, "_force_python", True)
        monkeypatch.setattr(video_clipper, "_use_python_clipper", lambda: True)
        mock_writer = MagicMock()
        with patch("cv2.VideoCapture", side_effect=lambda _: self._make_fake_cap(frames=3)), \
             patch("cv2.VideoWriter", return_value=mock_writer), \
             patch("cv2.VideoWriter_fourcc", return_value=0):
            stitch_clips(["a.mp4", "b.mp4"], "out.mp4")
        out = capsys.readouterr().out
        assert "[info]" in out


# ---------------------------------------------------------------------------
# -nostdin / stdin=DEVNULL — an ffmpeg inheriting an interactive stdin can
# otherwise wait on it forever (see force_ffmpeg fixture at module top).
# ---------------------------------------------------------------------------

class TestFfmpegStdinIsolation:
    def test_extract_clip_uses_nostdin_and_devnull(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("in.mp4", 0, 10, "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-nostdin" in cmd
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL

    def test_stitch_clips_uses_nostdin_and_devnull(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            stitch_clips(["a.mp4"], "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-nostdin" in cmd
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


# ---------------------------------------------------------------------------
# Cancellation — cancel_event routes through _run_ffmpeg's Popen-based path,
# never subprocess.run, so these tests mock Popen (or use a real short-lived
# process for the one true end-to-end check).
# ---------------------------------------------------------------------------

def _fake_popen(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


class TestCancellation:
    def test_extract_clip_raises_cancelled_when_event_preset(self, monkeypatch):
        # Event already set before ffmpeg's first poll — must cancel on the
        # very first iteration rather than waiting out a full timeout cycle.
        cancel_event = threading.Event()
        cancel_event.set()

        proc = MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=video_clipper._POLL_INTERVAL),
            ("", ""),  # the drain after terminate()
        ]
        with patch("subprocess.Popen", return_value=proc):
            with pytest.raises(RuntimeError, match="__cancelled__"):
                extract_clip("in.mp4", 0, 10, "out.mp4", cancel_event=cancel_event)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    def test_cancel_escalates_to_kill_if_terminate_does_not_exit(self):
        cancel_event = threading.Event()
        cancel_event.set()

        proc = MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=video_clipper._POLL_INTERVAL),
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5),  # terminate() didn't work
            ("", ""),  # final drain after kill()
        ]
        with patch("subprocess.Popen", return_value=proc):
            with pytest.raises(RuntimeError, match="__cancelled__"):
                video_clipper._run_ffmpeg(["ffmpeg", "-y"], cancel_event=cancel_event)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_create_clips_stops_between_clips_when_cancelled(self):
        # Cancelled after the first clip finishes: the second and third
        # clips must never be attempted, and Cancel must not silently no-op.
        cancel_event = threading.Event()
        calls = []

        def _popen_side_effect(cmd, **kw):
            calls.append(cmd)
            cancel_event.set()  # simulate Cancel firing right after clip 1
            return _fake_popen(returncode=0)

        with patch("subprocess.Popen", side_effect=_popen_side_effect):
            with tempfile.TemporaryDirectory() as tmpdir:
                with pytest.raises(RuntimeError, match="__cancelled__"):
                    create_clips(
                        "in.mp4", [(0, 10), (20, 30), (40, 50)], tmpdir,
                        cancel_event=cancel_event,
                    )
        assert len(calls) == 1

    def test_create_clips_status_callback_invoked_per_clip(self):
        messages = []
        with patch("subprocess.run", return_value=_ok()):
            with tempfile.TemporaryDirectory() as tmpdir:
                create_clips(
                    "in.mp4", [(0, 10), (20, 30)], tmpdir,
                    status_callback=messages.append,
                )
        assert len(messages) == 2
        assert "1/2" in messages[0]
        assert "2/2" in messages[1]

    def test_real_process_is_actually_killed_on_cancel(self, monkeypatch):
        # End-to-end check of the polling/terminate/kill mechanics against a
        # real child process (a short python sleep, portable across
        # platforms) rather than a mock — this is the actual guarantee the
        # fix provides: Cancel must not leave a wedged process behind.
        created = {}
        real_popen = subprocess.Popen

        def _tracking_popen(*a, **kw):
            p = real_popen(*a, **kw)
            created["proc"] = p
            return p

        monkeypatch.setattr(subprocess, "Popen", _tracking_popen)

        cancel_event = threading.Event()

        def _cancel_soon():
            time.sleep(0.3)
            cancel_event.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()

        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="__cancelled__"):
            video_clipper._run_ffmpeg(cmd, cancel_event=cancel_event)
        elapsed = time.monotonic() - start

        assert elapsed < 10, "cancellation should abort well before the 30s sleep completes"
        time.sleep(0.2)
        assert created["proc"].poll() is not None, "child process must not be left running"


# ---------------------------------------------------------------------------
# Match label overlay
# ---------------------------------------------------------------------------

class TestMatchLabel:
    def test_drawtext_filter_contents(self):
        f = video_clipper._drawtext_filter("Match 3", (0.25, 0.75), 40)
        assert f.startswith("drawtext=")
        assert "text='Match 3'" in f
        assert "fontsize=40" in f
        assert "w*0.2500-text_w/2" in f
        assert "h*0.7500-text_h/2" in f

    def test_drawtext_filter_escapes_commas_in_expressions(self):
        # Unescaped commas would split the -vf filtergraph.
        f = video_clipper._drawtext_filter("Match 1", (0.5, 0.5), 40)
        expr = f.split("x=", 1)[1]
        assert "," not in expr.replace("\\,", "")

    def test_label_reencodes_with_drawtext(self, monkeypatch):
        monkeypatch.setattr(video_clipper, "_video_height", lambda _: 1080)
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("in.mp4", 0, 10, "out.mp4", label="Match 1")
        cmd = mock_run.call_args[0][0]
        vf = cmd[cmd.index("-vf") + 1]
        assert "text='Match 1'" in vf
        assert "fontsize=54" in vf  # 1080 * LABEL_FONT_FRAC
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-c:a") + 1] == "copy"
        assert "-c" not in cmd

    def test_no_label_keeps_stream_copy(self):
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            extract_clip("in.mp4", 0, 10, "out.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-vf" not in cmd
        assert cmd[cmd.index("-c") + 1] == "copy"

    def test_create_clips_forwards_labels_in_order(self):
        with patch.object(video_clipper, "extract_clip") as mock_extract:
            with tempfile.TemporaryDirectory() as tmpdir:
                create_clips("in.mp4", [(0, 10), (20, 30)], tmpdir,
                             labels=["Match 1", "Match 2"], label_pos=(0.1, 0.9))
        labels = [c.kwargs["label"] for c in mock_extract.call_args_list]
        positions = {c.kwargs["label_pos"] for c in mock_extract.call_args_list}
        assert labels == ["Match 1", "Match 2"]
        assert positions == {(0.1, 0.9)}

    def test_create_clips_without_labels_passes_none(self):
        with patch.object(video_clipper, "extract_clip") as mock_extract:
            with tempfile.TemporaryDirectory() as tmpdir:
                create_clips("in.mp4", [(0, 10)], tmpdir)
        assert mock_extract.call_args.kwargs["label"] is None

    def test_python_fallback_draws_label(self, monkeypatch):
        import numpy as np
        monkeypatch.setattr(video_clipper, "_use_python_clipper", lambda: True)
        frame = np.zeros((200, 320, 3), dtype=np.uint8)
        cap = MagicMock()
        cap.get.side_effect = lambda prop: {0: 30.0, 3: 320, 4: 200}.get(prop, 0)
        cap.read.side_effect = [(True, frame), (False, None)]
        writer = MagicMock()
        with patch("cv2.VideoCapture", return_value=cap), \
             patch("cv2.VideoWriter", return_value=writer), \
             patch("cv2.VideoWriter_fourcc", return_value=0):
            extract_clip("in.mp4", 0, 1, "out.mp4", label="Match 1", label_pos=(0.5, 0.5))
        written = writer.write.call_args[0][0]
        assert written.max() == 255  # white label text drawn into the frame
