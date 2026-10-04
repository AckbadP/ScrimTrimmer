"""
video_clipper.py - Extract clips and stitch them using ffmpeg.

Falls back to OpenCV-based Python processing when ffmpeg is not available.
Note: the Python fallback re-encodes video and does NOT preserve audio.
"""

import os
import shutil
import subprocess
import tempfile
import threading
from typing import List, Optional, Tuple


_force_python: bool = False

# How often to poll a running ffmpeg process for cancellation.
_POLL_INTERVAL = 0.5


def _run_ffmpeg(cmd: List[str], cancel_event: Optional[threading.Event] = None):
    """
    Run an ffmpeg command and return a subprocess.CompletedProcess-like result
    with .returncode and .stderr.

    stdin is always redirected to DEVNULL with -nostdin already in `cmd` — an
    ffmpeg inheriting an interactive stdin (e.g. under a console-mode build)
    can otherwise wait on it indefinitely.

    When `cancel_event` is given, the process is polled instead of waited on
    with a single blocking call, so it can be terminated (then killed) as
    soon as the event is set — without this, Cancel has no effect once
    clip extraction/stitching has started.
    """
    if cancel_event is None:
        return subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_POLL_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                    raise RuntimeError("__cancelled__")
    except RuntimeError:
        raise
    except BaseException:
        # Any other failure (e.g. the calling thread being torn down) — make
        # sure we don't leak the ffmpeg child.
        proc.kill()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def set_force_python(value: bool) -> None:
    """Force the Python/OpenCV fallback even when ffmpeg is available."""
    global _force_python
    _force_python = value


def _use_python_clipper() -> bool:
    return _force_python or shutil.which("ffmpeg") is None


# ---------------------------------------------------------------------------
# ffmpeg implementations
# ---------------------------------------------------------------------------

def _extract_clip_ffmpeg(
    input_path: str,
    start_sec: int,
    end_sec: int,
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", input_path,
        "-c", "copy",
        output_path,
    ]
    result = _run_ffmpeg(cmd, cancel_event)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed extracting clip {start_sec}–{end_sec}s:\n{result.stderr}"
        )


def _stitch_clips_ffmpeg(
    clip_paths: List[str],
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as f:
        for path in clip_paths:
            f.write(f"file '{os.path.abspath(path)}'\n")
        concat_list_path = f.name

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            output_path,
        ]
        result = _run_ffmpeg(cmd, cancel_event)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed:\n{result.stderr}")
    finally:
        os.unlink(concat_list_path)


# ---------------------------------------------------------------------------
# Python/OpenCV fallback implementations (no audio, re-encodes)
# ---------------------------------------------------------------------------

def _extract_clip_python(
    input_path: str,
    start_sec: int,
    end_sec: int,
    output_path: str,
) -> None:
    """Extract a clip using OpenCV. Re-encodes; audio is not preserved."""
    import cv2

    cap = cv2.VideoCapture(input_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    start_frame = int(start_sec * fps)
    end_frame = int(end_sec * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    for _ in range(end_frame - start_frame):
        ret, frame = cap.read()
        if not ret:
            break
        writer.write(frame)

    cap.release()
    writer.release()


def _stitch_clips_python(clip_paths: List[str], output_path: str) -> None:
    """Concatenate clips using OpenCV. Re-encodes; audio is not preserved."""
    import cv2

    cap0 = cv2.VideoCapture(clip_paths[0])
    fps = cap0.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap0.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap0.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap0.release()

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    for clip_path in clip_paths:
        cap = cv2.VideoCapture(clip_path)
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            writer.write(frame)
        cap.release()

    writer.release()


# ---------------------------------------------------------------------------
# Public API — dispatches to ffmpeg or Python fallback
# ---------------------------------------------------------------------------

def extract_clip(
    input_path: str,
    start_sec: int,
    end_sec: int,
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """
    Extract a clip from a video file.

    Uses ffmpeg (stream-copy, with audio) when available, otherwise falls back
    to OpenCV (re-encodes, no audio).

    Args:
        input_path: Source video path (.mp4 or .mkv).
        start_sec: Clip start time in seconds.
        end_sec: Clip end time in seconds.
        output_path: Destination path for the extracted clip.
        cancel_event: If set while ffmpeg is running, the process is
            terminated (then killed) and RuntimeError("__cancelled__") is
            raised. Ignored by the Python fallback.
    """
    if not _use_python_clipper():
        _extract_clip_ffmpeg(input_path, start_sec, end_sec, output_path, cancel_event)
    else:
        if _force_python:
            print("  [info] Python clipper forced — re-encoding, no audio")
        else:
            print("  [warning] ffmpeg not found — using Python fallback (no audio, re-encodes)")
        _extract_clip_python(input_path, start_sec, end_sec, output_path)


def stitch_clips(
    clip_paths: List[str],
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """
    Concatenate multiple clips into a single video.

    Uses ffmpeg (stream-copy, with audio) when available, otherwise falls back
    to OpenCV (re-encodes, no audio).

    Args:
        clip_paths: Ordered list of clip paths to concatenate.
        output_path: Destination path for the final stitched video.
        cancel_event: If set while ffmpeg is running, the process is
            terminated (then killed) and RuntimeError("__cancelled__") is
            raised. Ignored by the Python fallback.
    """
    if not clip_paths:
        raise ValueError("No clips to stitch.")

    if not _use_python_clipper():
        _stitch_clips_ffmpeg(clip_paths, output_path, cancel_event)
    else:
        if _force_python:
            print("  [info] Python clipper forced — re-encoding, no audio")
        else:
            print("  [warning] ffmpeg not found — using Python fallback (no audio, re-encodes)")
        _stitch_clips_python(clip_paths, output_path)


def create_clips(
    input_path: str,
    pairs: List[Tuple[int, int]],
    output_dir: str,
    cancel_event: Optional[threading.Event] = None,
    status_callback=None,
) -> List[str]:
    """
    Extract all (cd, wf) clips from the input video.

    Args:
        input_path: Source video path (.mp4 or .mkv).
        pairs: List of (start_sec, end_sec) tuples.
        output_dir: Directory to save individual clips.
        cancel_event: Checked between clips (and inside each ffmpeg call);
            raises RuntimeError("__cancelled__") when set.
        status_callback: Optional callable(str) invoked with a human-readable
            "Extracting clip i/n" message before each clip.

    Returns:
        Ordered list of clip file paths.
    """
    os.makedirs(output_dir, exist_ok=True)
    clip_paths = []
    total = len(pairs)

    for i, (start, end) in enumerate(pairs, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("__cancelled__")
        clip_path = os.path.join(output_dir, f"clip_{i:03d}.mp4")
        print(f"  Extracting clip {i}: {start}s → {end}s → {clip_path}")
        if status_callback is not None:
            status_callback(f"Extracting clip {i}/{total}...")
        extract_clip(input_path, start, end, clip_path, cancel_event)
        clip_paths.append(clip_path)

    return clip_paths


def _extract_audio_ffmpeg(
    input_path: str,
    start_sec: int,
    end_sec: int,
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", input_path,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        output_path,
    ]
    result = _run_ffmpeg(cmd, cancel_event)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed extracting audio {start_sec}–{end_sec}s:\n{result.stderr}"
        )


def create_audio_clips(
    input_path: str,
    pairs: List[Tuple[int, int]],
    output_dir: str,
    cancel_event: Optional[threading.Event] = None,
    status_callback=None,
) -> List[str]:
    """
    Extract the audio of each (start, end) pair from the input video as MP3.

    Requires ffmpeg; when the Python fallback is in use a warning is printed
    and nothing is extracted.

    Args:
        input_path: Source video path (.mp4 or .mkv).
        pairs: List of (start_sec, end_sec) tuples.
        output_dir: Directory to save the audio files (created if missing).
        cancel_event: Checked between files (and inside each ffmpeg call);
            raises RuntimeError("__cancelled__") when set.
        status_callback: Optional callable(str) invoked with a human-readable
            "Extracting audio i/n" message before each file.

    Returns:
        Ordered list of audio file paths (empty if ffmpeg is unavailable).
    """
    if _use_python_clipper():
        print("  [warning] Audio extraction requires ffmpeg — skipping")
        return []

    os.makedirs(output_dir, exist_ok=True)
    audio_paths = []
    total = len(pairs)

    for i, (start, end) in enumerate(pairs, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("__cancelled__")
        audio_path = os.path.join(output_dir, f"match_{i:03d}.mp3")
        print(f"  Extracting audio {i}: {start}s → {end}s → {audio_path}")
        if status_callback is not None:
            status_callback(f"Extracting audio {i}/{total}...")
        _extract_audio_ffmpeg(input_path, start, end, audio_path, cancel_event)
        audio_paths.append(audio_path)

    return audio_paths
