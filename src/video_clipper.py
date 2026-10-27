"""
video_clipper.py - Extract clips and stitch them using ffmpeg.

Falls back to OpenCV-based Python processing when ffmpeg is not available.
Note: the Python fallback re-encodes video and does NOT preserve audio.
"""

import os
import platform
import shutil
import subprocess
import tempfile
import threading
from typing import List, Optional, Tuple


_force_python: bool = False

# How often to poll a running ffmpeg process for cancellation.
_POLL_INTERVAL = 0.5

# Match label font size as a fraction of frame height. Shared with the GUI
# preview so the on-thumbnail label matches the rendered output.
LABEL_FONT_FRAC = 0.05
DEFAULT_LABEL_POS = (0.5, 0.05)  # label center as (x, y) frame fractions

_LABEL_PAD = 8  # px of dark box around the label text

_WINDOWS_FONT = r"C:\Windows\Fonts\arial.ttf"


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


def _video_height(path: str) -> int:
    """Frame height of `path` in pixels, or 1080 if it can't be read."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return height if height > 0 else 1080
    except Exception:
        return 1080


def _label_font_size(frame_height: int) -> int:
    return max(8, int(frame_height * LABEL_FONT_FRAC))


# ---------------------------------------------------------------------------
# ffmpeg implementations
# ---------------------------------------------------------------------------

def _drawtext_filter(text: str, pos: Tuple[float, float], fontsize: int) -> str:
    """
    Build an ffmpeg drawtext filter rendering `text` centered at `pos`
    (frame fractions), clamped so the label stays fully inside the frame.
    """
    fx, fy = pos
    pad = _LABEL_PAD
    # drawtext's own escaping: backslash, colon and quote are special.
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    parts = [
        f"text='{safe}'",
        f"fontsize={fontsize}",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.5",
        f"boxborderw={pad}",
        f"x=max({pad}\\,min(w-text_w-{pad}\\,w*{fx:.4f}-text_w/2))",
        f"y=max({pad}\\,min(h-text_h-{pad}\\,h*{fy:.4f}-text_h/2))",
    ]
    # Windows ffmpeg builds can't always resolve a default font via
    # fontconfig, so point at a font that ships with the OS.
    if platform.system() == "Windows" and os.path.isfile(_WINDOWS_FONT):
        parts.insert(0, "fontfile='C\\:/Windows/Fonts/arial.ttf'")
    return "drawtext=" + ":".join(parts)


def _extract_clip_ffmpeg(
    input_path: str,
    start_sec: int,
    end_sec: int,
    output_path: str,
    cancel_event: Optional[threading.Event] = None,
    label: Optional[str] = None,
    label_pos: Tuple[float, float] = DEFAULT_LABEL_POS,
) -> None:
    if label:
        # Burning in text needs a re-encode; audio can still be copied.
        # Every labelled clip uses the same encoder settings, so the concat
        # stitch can still stream-copy them.
        fontsize = _label_font_size(_video_height(input_path))
        codec_args = [
            "-vf", _drawtext_filter(label, label_pos, fontsize),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "copy",
        ]
    else:
        codec_args = ["-c", "copy"]
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", input_path,
        *codec_args,
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

def _draw_label_cv2(frame, text: str, pos: Tuple[float, float], fontsize: int) -> None:
    """Draw `text` centered at `pos` on a BGR frame, in place, over a dark box."""
    import cv2

    h, w = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    # Hershey caps are ~22px tall at scale 1.0; drawtext's fontsize is the
    # em size, whose caps are ~0.7 of it — match that so both paths agree.
    scale = fontsize * 0.7 / 22.0
    thickness = max(1, int(round(scale * 1.5)))
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    pad = _LABEL_PAD
    x = int(min(max(w * pos[0] - tw / 2, pad), w - tw - pad))
    y = int(min(max(h * pos[1] + th / 2, th + pad), h - baseline - pad))
    overlay = frame.copy()
    cv2.rectangle(overlay, (x - pad, y - th - pad), (x + tw + pad, y + baseline + pad),
                  (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, dst=frame)
    cv2.putText(frame, text, (x, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)


def _extract_clip_python(
    input_path: str,
    start_sec: int,
    end_sec: int,
    output_path: str,
    label: Optional[str] = None,
    label_pos: Tuple[float, float] = DEFAULT_LABEL_POS,
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

    fontsize = _label_font_size(height)
    for _ in range(end_frame - start_frame):
        ret, frame = cap.read()
        if not ret:
            break
        if label:
            _draw_label_cv2(frame, label, label_pos, fontsize)
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
    label: Optional[str] = None,
    label_pos: Tuple[float, float] = DEFAULT_LABEL_POS,
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
        label: If given, burn this text into the clip (forces a re-encode
            on the ffmpeg path).
        label_pos: Label center as (x, y) frame fractions.
    """
    if not _use_python_clipper():
        _extract_clip_ffmpeg(input_path, start_sec, end_sec, output_path, cancel_event,
                             label=label, label_pos=label_pos)
    else:
        if _force_python:
            print("  [info] Python clipper forced — re-encoding, no audio")
        else:
            print("  [warning] ffmpeg not found — using Python fallback (no audio, re-encodes)")
        _extract_clip_python(input_path, start_sec, end_sec, output_path,
                             label=label, label_pos=label_pos)


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
    labels: Optional[List[str]] = None,
    label_pos: Tuple[float, float] = DEFAULT_LABEL_POS,
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
        labels: Optional per-clip text burned into each clip (same order
            as `pairs`); None leaves clips unlabelled.
        label_pos: Label center as (x, y) frame fractions.

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
        label = labels[i - 1] if labels else None
        extract_clip(input_path, start, end, clip_path, cancel_event,
                     label=label, label_pos=label_pos)
        clip_paths.append(clip_path)

    return clip_paths
