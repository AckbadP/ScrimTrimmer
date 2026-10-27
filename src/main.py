"""
main.py - CLI entry point for the EVE AT Practice Trimmer.

Pipeline:
  1. Extract 1 frame/sec from the input video
  2. Run OCR on the left chat window region of each frame
  3. Detect new CD and WF occurrences, pair them up
  4. Extract CD→WF clips with ffmpeg
  5. Stitch all clips into a final output video
"""

import argparse
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

import numpy as np

from frame_extractor import check_source_video, extract_frames, get_video_duration
from ocr_processor import crop_chat_region, run_ocr_on_region
from chat_analyzer import analyze_frames, pair_cd_wf
from chat_log_parser import parse_chat_logs, game_time_to_seconds
from log_matcher import detect_t0
import video_clipper
from video_clipper import DEFAULT_LABEL_POS, create_clips, stitch_clips


class CancelledError(Exception):
    """Raised when the user cancels the pipeline."""


class MissingDependencyError(Exception):
    """Raised when a required external tool is not found."""

    def __init__(self, tool: str, message: str):
        super().__init__(message)
        self.tool = tool


class UnfinalizedSourceError(Exception):
    """
    Raised when the source video looks like it is still being written (e.g.
    the game client / recorder is still running) or is otherwise missing a
    valid container index. Processing such a file is unreliable and can
    make clip extraction extremely slow or hang-looking; the GUI offers to
    proceed anyway, which re-invokes the pipeline with `force_unfinalized`.
    """


def _check_cancel(args):
    cancel_event = getattr(args, 'cancel_event', None)
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Cancelled by user")


def _warn_orphans(
    cd_times: List[int],
    wf_times: List[int],
    pairs: List[Tuple[int, int]],
) -> None:
    paired_cds = {cd for cd, _ in pairs}
    paired_wfs = {wf for _, wf in pairs}
    orphan_cds = [t for t in cd_times if t not in paired_cds]
    orphan_wfs = [t for t in wf_times if t not in paired_wfs]
    if orphan_cds:
        print(f"  Warning: {len(orphan_cds)} CD(s) had no matching WF and were skipped: {orphan_cds}", file=sys.stderr)
    if orphan_wfs:
        print(f"  Warning: {len(orphan_wfs)} WF(s) had no preceding CD and were skipped: {orphan_wfs}", file=sys.stderr)
        print(f"  {len(orphan_wfs)} round(s) had no CD/countdown and were skipped")


def apply_buffers(
    pairs: List[Tuple[int, int]],
    start_buf: int,
    end_buf: int,
    duration: int,
) -> List[Tuple[int, int]]:
    """
    Shift each (start, end) pair by the given buffers, clamped to
    [0, duration]. A negative buffer moves the edge earlier; positive
    moves it later. Pairs that collapse (end <= start) after clamping
    are dropped.
    """
    result = []
    dropped = 0
    for start, end in pairs:
        new_start = min(max(start + start_buf, 0), duration)
        new_end = min(max(end + end_buf, 0), duration)
        if new_end <= new_start:
            dropped += 1
            continue
        result.append((new_start, new_end))
    if dropped:
        print(f"  Warning: {dropped} clip(s) collapsed after applying buffers and were skipped", file=sys.stderr)
    return result


def _buffers_for(args) -> Tuple[int, int]:
    """Return (start_buf, end_buf) for the current mode (normal vs tournament)."""
    if getattr(args, "tournament_match", False):
        return (
            getattr(args, "tournament_buffer_start", 0),
            getattr(args, "tournament_buffer_end", 10),
        )
    return (
        getattr(args, "buffer_start", 0),
        getattr(args, "buffer_end", 0),
    )


def _find_windows_tool(name: str) -> "str | None":
    """
    Probe common Windows install directories for an executable not in PATH.
    Returns the directory containing the exe, or None if not found.
    """
    import platform
    if platform.system() != "Windows":
        return None
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    title = name.title()
    candidates = [
        os.path.join(pf, f"{title}-OCR"),
        os.path.join(pf, title),
        os.path.join(pf86, title),
        os.path.join(local, "Programs", f"{title}-OCR"),
        os.path.join(local, "Programs", title),
        # ffmpeg manual installs
        rf"C:\{name}\bin",
        rf"C:\{name}-release-full\bin",
    ]
    exe = name + ".exe"
    for directory in candidates:
        if directory and os.path.isfile(os.path.join(directory, exe)):
            return directory
    return None


def _check_dependencies(args) -> None:
    """Verify required external tools are available before starting a long job."""
    # Raw mode never cuts the video, so ffmpeg isn't needed.
    needs_ffmpeg = not (getattr(args, "run_without_ffmpeg", False) or getattr(args, "raw_mode", False))
    if needs_ffmpeg and shutil.which("ffmpeg") is None:
        ffmpeg_dir = _find_windows_tool("ffmpeg")
        if ffmpeg_dir:
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
        else:
            raise MissingDependencyError(
                "ffmpeg",
                "ffmpeg not found in PATH. Install ffmpeg and try again.",
            )
    if shutil.which("tesseract") is None:
        tess_dir = _find_windows_tool("tesseract")
        if tess_dir:
            os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")
            try:
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = os.path.join(tess_dir, "tesseract.exe")
            except ImportError:
                pass
        else:
            raise MissingDependencyError(
                "tesseract",
                "tesseract not found in PATH. Install tesseract-ocr and try again.",
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trim EVE Online AT practice footage using OCR-detected CD/WF commands."
    )
    parser.add_argument("video", help="Path to the input video file (.mp4 or .mkv)")
    parser.add_argument(
        "--output", "-o",
        default="out",
        help="Output directory for clips and final video (default: out/)",
    )
    parser.add_argument(
        "--chat-region",
        type=float,
        nargs=4,
        metavar=("X1", "Y1", "X2", "Y2"),
        default=[0.0, 0.35, 0.15, 1.0],
        help="Chat window region as fractions: x1 y1 x2 y2 (default: 0.0 0.35 0.15 1.0)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        metavar="N",
        help="Number of OCR worker threads (default: number of CPU cores)",
    )
    parser.add_argument(
        "--ram-cap-gb",
        type=int,
        default=10,
        metavar="GB",
        dest="ram_cap_gb",
        help="Maximum GB of video frames to hold in memory at once (default: 10)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print OCR text and detection info per frame",
    )
    parser.add_argument(
        "--chat-log",
        action="append",
        metavar="FILE",
        dest="chat_logs",
        help="EVE chat log file to use instead of OCR (may be specified multiple times)",
    )
    parser.add_argument(
        "--t0",
        metavar="HH:MM:SS",
        help=(
            "EVE game time (UTC) at video second 0. "
            "Required with --chat-log unless it can be auto-detected from the video."
        ),
    )
    parser.add_argument(
        "--chapters-dir",
        default=None,
        metavar="DIR",
        dest="chapters_dir",
        help="Directory to save the YouTube chapters .txt file (default: same as output dir)",
    )
    parser.add_argument(
        "--run-without-ffmpeg",
        action="store_true",
        dest="run_without_ffmpeg",
        help=(
            "Use the Python/OpenCV clipper instead of ffmpeg. "
            "Slower, re-encodes video, and strips audio. "
            "Useful when ffmpeg is not available."
        ),
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        dest="force_ocr",
        help=(
            "Force OCR pipeline even when --chat-log is provided. "
            "Ignores chat logs and detects CD/WF events via OCR instead."
        ),
    )
    parser.add_argument(
        "--tournament-match",
        action="store_true",
        dest="tournament_match",
        help=(
            "Detect tournament match boundaries instead of CD/WF commands. "
            "Start: 'EVE System > 30 seconds until match start...'. "
            "End: 'EVE System > Match completed!'."
        ),
    )
    parser.add_argument(
        "--buffer-start",
        type=int,
        default=0,
        metavar="SECONDS",
        dest="buffer_start",
        help="Seconds added to each clip's start in normal mode (negative = earlier; default: 0)",
    )
    parser.add_argument(
        "--buffer-end",
        type=int,
        default=0,
        metavar="SECONDS",
        dest="buffer_end",
        help="Seconds added to each clip's end in normal mode (negative = earlier; default: 0)",
    )
    parser.add_argument(
        "--tournament-buffer-start",
        type=int,
        default=0,
        metavar="SECONDS",
        dest="tournament_buffer_start",
        help="Seconds added to each clip's start in tournament mode (negative = earlier; default: 0)",
    )
    parser.add_argument(
        "--tournament-buffer-end",
        type=int,
        default=10,
        metavar="SECONDS",
        dest="tournament_buffer_end",
        help="Seconds added to each clip's end in tournament mode (negative = earlier; default: 10)",
    )
    parser.add_argument(
        "--no-detect-countdown",
        action="store_false",
        dest="detect_countdown",
        default=True,
        help=(
            "Disable treating a bare descending countdown ('10', '9', '8', ...) "
            "as an implicit CD. On by default so rounds where the caller skips "
            "the 'CD' announcement still get a clip start."
        ),
    )
    parser.add_argument(
        "--match-label",
        action="store_true",
        dest="match_label",
        help=(
            "Burn a 'Match N' label (same names as the YouTube chapters) into "
            "each clip. Re-encodes the video, so it is slower than the default."
        ),
    )
    parser.add_argument(
        "--match-label-pos",
        type=float,
        nargs=2,
        metavar=("X", "Y"),
        default=list(DEFAULT_LABEL_POS),
        dest="match_label_pos",
        help=(
            "Center of the match label as frame fractions "
            f"(default: {DEFAULT_LABEL_POS[0]} {DEFAULT_LABEL_POS[1]} = top center)"
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        dest="raw_mode",
        help=(
            "Detect matches but don't cut the video. Chapters use source-video "
            "timestamps, and YouTube upload (GUI) sends the full source video."
        ),
    )
    return parser.parse_args()


def _fmt_duration(seconds: float) -> str:
    """Format seconds as h:mm:ss or m:ss."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def chapter_titles(count: int) -> List[str]:
    """Titles shared by the YouTube chapters and the on-video match labels."""
    return [f"Match {i}" for i in range(1, count + 1)]


def _label_kwargs(args, count: int) -> dict:
    """create_clips() label kwargs when --match-label is on, else {}."""
    if not getattr(args, "match_label", False):
        return {}
    pos = getattr(args, "match_label_pos", None) or DEFAULT_LABEL_POS
    return {"labels": chapter_titles(count), "label_pos": tuple(pos)}


def write_chapter_timestamps(
    pairs: List[Tuple[int, int]],
    output_path: str,
    chapters_dir: str | None = None,
) -> str:
    """
    Write a YouTube-compatible chapter timestamps file.

    Each line is "<timestamp> <title>" (see chapter_titles), where the timestamp is the
    cumulative start time of that clip in the stitched output video.
    The first chapter always starts at 0:00, as required by YouTube.

    Args:
        pairs: Ordered list of (start_sec, end_sec) tuples used to build clips.
        output_path: Path to the stitched output video; used to derive the
                     default .txt filename.
        chapters_dir: Directory to write the .txt file. If None or empty,
                      the file is saved alongside output_path.

    Returns:
        Path to the written .txt file.
    """
    cumulative = 0
    lines = []
    for title, (start, end) in zip(chapter_titles(len(pairs)), pairs):
        lines.append(f"{_fmt_duration(cumulative)} {title}")
        cumulative += end - start

    return _write_chapters_file(lines, output_path, chapters_dir)


def _write_chapters_file(lines: List[str], output_path: str, chapters_dir: str | None) -> str:
    """Write chapter lines to <output basename>_chapters.txt; return its path."""
    base_name = os.path.splitext(os.path.basename(output_path))[0] + "_chapters.txt"
    if chapters_dir:
        os.makedirs(chapters_dir, exist_ok=True)
        txt_path = os.path.join(chapters_dir, base_name)
    else:
        txt_path = os.path.join(os.path.dirname(output_path), base_name)
    with open(txt_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return txt_path


# YouTube ignores chapters shorter than this, so gaps below it are folded
# into the neighbouring chapter instead of getting their own marker.
_MIN_CHAPTER_SEC = 10


def raw_chapter_lines(pairs: List[Tuple[int, int]], duration: int) -> List[str]:
    """
    Chapter lines for raw mode, where the uncut source video is uploaded.

    Timestamps are positions in the source video. Each match gets a
    "Match N" chapter at its start; downtime gets "Pre-match", "Break" or
    "Post-match" chapters when it's at least _MIN_CHAPTER_SEC long. The
    first chapter always starts at 0:00, as required by YouTube.
    """
    chapters: List[Tuple[int, str]] = []
    titles = chapter_titles(len(pairs))
    for i, (title, (start, end)) in enumerate(zip(titles, pairs)):
        if i == 0:
            if start >= _MIN_CHAPTER_SEC:
                chapters.append((0, "Pre-match"))
            else:
                start = 0
        chapters.append((start, title))
        next_start = pairs[i + 1][0] if i + 1 < len(pairs) else duration
        if next_start - end >= _MIN_CHAPTER_SEC:
            chapters.append((end, "Break" if i + 1 < len(pairs) else "Post-match"))
    return [f"{_fmt_duration(t)} {title}" for t, title in chapters]


def write_raw_chapter_timestamps(
    pairs: List[Tuple[int, int]],
    duration: int,
    output_path: str,
    chapters_dir: str | None = None,
) -> str:
    """Write raw-mode chapters (see raw_chapter_lines); return the .txt path."""
    return _write_chapters_file(raw_chapter_lines(pairs, duration), output_path, chapters_dir)


def _progress_bar(current: int, total: int, elapsed: float, width: int = 30) -> str:
    """Return a one-line progress string with bar, percent, and ETA."""
    pct = current / total if total > 0 else 0
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    eta_str = ""
    if current > 0 and elapsed > 0:
        rate = current / elapsed  # frames per second
        remaining = (total - current) / rate
        eta_str = f"  ETA {_fmt_duration(remaining)}"
    return f"\r  [{bar}] {pct*100:5.1f}%  {current}/{total}s{eta_str}  "


def output_video_path(args, output_dir: str) -> str:
    """Final video path: named after the YouTube title (sanitised), else final_output.mp4."""
    title = (getattr(args, "youtube_title", "") or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return os.path.join(output_dir, f"{name or 'final_output'}.mp4")


def _maybe_upload(
    args,
    video_path: str,
    description: str,
    notify,
) -> "str | None":
    """Upload to YouTube if enabled in args. Returns URL or None."""
    if not getattr(args, "youtube_upload", False):
        return None
    cancel_event = getattr(args, "cancel_event", None)
    progress_cb = getattr(args, "progress_callback", None)
    try:
        from youtube_uploader import upload as yt_upload
        title = (getattr(args, "youtube_title", "") or "").strip()
        if not title:
            title = os.path.splitext(os.path.basename(args.video))[0]
        notify("Uploading to YouTube...")
        print("\nUploading to YouTube...")
        if progress_cb:
            progress_cb(0, 100)
        def _yt_progress(pct: int):
            if progress_cb:
                progress_cb(pct, 100)
        url = yt_upload(video_path, title, description, status_callback=_yt_progress, cancel_event=cancel_event)
        notify(f"Uploaded: {url}")
        print(f"YouTube: {url}")
        return url
    except RuntimeError as exc:
        msg = str(exc)
        if cancel_event is not None and cancel_event.is_set():
            notify("YouTube upload cancelled")
            print("YouTube upload cancelled", file=sys.stderr)
        else:
            notify(f"YouTube upload failed: {msg}")
            print(f"YouTube upload failed: {msg}", file=sys.stderr)
        return None
    except Exception as exc:
        msg = str(exc)
        notify(f"YouTube upload failed: {msg}")
        print(f"YouTube upload failed: {msg}", file=sys.stderr)
        return None


def _produce_output(args, pairs, duration: int, output_dir: str, notify, first_step: int):
    """
    Turn detected (start, end) pairs into the final output: cut + stitch
    clips and write stitched-video chapters, or in raw mode just write
    source-video chapters. Uploads if enabled. Returns
    (chapters_text, youtube_url).
    """
    final_output = output_video_path(args, output_dir)
    chapters_dir = getattr(args, "chapters_dir", None)

    if getattr(args, "raw_mode", False):
        print(f"\n[{first_step}/4] Raw mode: skipping clip extraction")
        if getattr(args, "match_label", False):
            print("  [info] Match label is ignored in raw mode (the video is not re-encoded)")
        chapters_path = write_raw_chapter_timestamps(pairs, duration, final_output, chapters_dir)
        upload_path = args.video
        print(f"\nDone! Source video (uncut): {upload_path}")
    else:
        _check_cancel(args)
        notify(f"Extracting {len(pairs)} clip(s)...")
        print(f"\n[{first_step}/4] Extracting {len(pairs)} clip(s)...")
        try:
            clip_paths = create_clips(
                args.video, pairs, output_dir,
                cancel_event=getattr(args, "cancel_event", None),
                status_callback=notify,
                **_label_kwargs(args, len(pairs)),
            )

            _check_cancel(args)
            notify("Stitching clips...")
            print(f"\n[{first_step + 1}/4] Stitching clips into {final_output}...")
            stitch_clips(clip_paths, final_output, cancel_event=getattr(args, "cancel_event", None))
        except RuntimeError as e:
            if str(e) == "__cancelled__":
                raise CancelledError("Cancelled by user")
            raise

        for clip in clip_paths:
            os.remove(clip)

        chapters_path = write_chapter_timestamps(pairs, final_output, chapters_dir)
        upload_path = final_output
        print(f"\nDone! Final video: {final_output}")

    print(f"Chapter timestamps:  {chapters_path}")
    with open(chapters_path) as _f:
        chapters_text = _f.read()

    youtube_url = _maybe_upload(args, upload_path, chapters_text, notify)
    return chapters_text, youtube_url


def run(args) -> None:
    """Run the trimmer pipeline with a pre-built args namespace."""
    video_clipper.set_force_python(getattr(args, "run_without_ffmpeg", False))
    _check_dependencies(args)

    _status_cb = getattr(args, 'status_callback', None)

    def _notify(msg: str):
        if _status_cb:
            _status_cb(msg)

    args.video = os.path.expanduser(args.video)
    args.output = os.path.expanduser(args.output)
    if args.chapters_dir:
        args.chapters_dir = os.path.expanduser(args.chapters_dir)
    if args.chat_logs:
        args.chat_logs = [os.path.expanduser(p) for p in args.chat_logs]

    if not os.path.isfile(args.video):
        print(f"Error: video file not found: {args.video}", file=sys.stderr)
        sys.exit(1)

    if not getattr(args, "force_unfinalized", False):
        _notify("Checking source video...")
        warning = check_source_video(args.video)
        if warning:
            if _status_cb is not None:
                # Running under the GUI — let it ask the user whether to
                # proceed rather than deciding unilaterally.
                raise UnfinalizedSourceError(warning)
            print(f"Warning: {warning}", file=sys.stderr)

    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)

    duration = int(get_video_duration(args.video))
    print(f"Processing: {args.video}  ({_fmt_duration(duration)})")

    # --- Chat log mode: skip OCR entirely ---
    if args.chat_logs and not getattr(args, "force_ocr", False):
        if args.t0:
            try:
                t0_sec = game_time_to_seconds(args.t0)
            except ValueError as e:
                print(f"Error: invalid --t0 value: {e}", file=sys.stderr)
                sys.exit(1)
            t0_source = f"t0={args.t0} (provided)"
        else:
            _check_cancel(args)
            _notify("Auto-detecting t0 from chat log(s)...")
            print(f"\n[0/4] Auto-detecting t0 from chat log(s) "
                  f"(sampling 1 frame per 30 s)...")
            try:
                t0_sec = detect_t0(
                    args.chat_logs,
                    args.video,
                    chat_region=tuple(args.chat_region),
                    verbose=args.verbose,
                    progress_callback=getattr(args, 'progress_callback', None),
                    cancel_event=getattr(args, 'cancel_event', None),
                )
            except RuntimeError as e:
                if str(e) == "__cancelled__":
                    raise CancelledError("Cancelled by user")
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            hh = t0_sec // 3600
            mm = (t0_sec % 3600) // 60
            ss = t0_sec % 60
            t0_source = f"t0={hh:02d}:{mm:02d}:{ss:02d} (auto-detected)"

        print(f"Chat log mode: {t0_source}  ({len(args.chat_logs)} log file(s))")
        _notify("Parsing chat log(s)...")
        print("\n[1/4] Parsing chat log(s)...")
        _tourn = getattr(args, "tournament_match", False)
        cd_times, wf_times = parse_chat_logs(args.chat_logs, t0_sec, duration,
                                               tournament_mode=_tourn,
                                               detect_countdown=getattr(args, "detect_countdown", True))
        print(f"  Found {len(cd_times)} CD(s) at: {cd_times}")
        print(f"  Found {len(wf_times)} WF(s) at: {wf_times}")

        pairs = pair_cd_wf(cd_times, wf_times)
        print(f"  Paired {len(pairs)} clip(s): {pairs}")

        _warn_orphans(cd_times, wf_times, pairs)

        start_buf, end_buf = _buffers_for(args)
        if start_buf or end_buf:
            pairs = apply_buffers(pairs, start_buf, end_buf, duration)
            print(f"  Applied buffers (start={start_buf:+d}s, end={end_buf:+d}s): {pairs}")

        if not pairs:
            print("\nNo CD→WF pairs found. Nothing to clip.", file=sys.stderr)
            sys.exit(0)

        return _produce_output(args, pairs, duration, output_dir, _notify, first_step=2)

    # --- OCR mode ---
    x1, y1, x2, y2 = args.chat_region
    print(f"Chat region: ({x1:.2f},{y1:.2f}) → ({x2:.2f},{y2:.2f})")

    # Step 1 & 2: Extract frames and run OCR
    workers = args.threads if (getattr(args, "threads", None) and args.threads > 0) else (os.cpu_count() or 4)
    _notify(f"Extracting frames and running OCR ({duration} frames, {workers} threads)...")
    print(f"\n[1/4] Extracting frames and running OCR ({duration} frames, {workers} threads)...")
    start_time = time.monotonic()

    results: dict = {}
    results_lock = threading.Lock()
    completed_count = [0]
    failed_count = [0]
    sem: threading.Semaphore | None = None

    def _make_callback(s: int):
        def callback(fut):
            # `finally: sem.release()` is load-bearing — if a worker raises
            # (bad frame, tesseract timeout, etc.) and we skip the release,
            # the producer eventually deadlocks forever at sem.acquire().
            try:
                with results_lock:
                    exc = fut.exception()
                    if exc is not None:
                        results[s] = ""
                        failed_count[0] += 1
                    else:
                        results[s] = fut.result()
                    completed_count[0] += 1
                    if not args.verbose:
                        elapsed = time.monotonic() - start_time
                        sys.stdout.write(_progress_bar(completed_count[0], duration, elapsed))
                        sys.stdout.flush()
            finally:
                sem.release()
        return callback

    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        for second, frame in extract_frames(args.video):
            _check_cancel(args)
            # Copy the crop out of the full-frame numpy view: `region.base`
            # would otherwise keep the *entire* decoded frame alive, so the
            # in-flight cap below (sized from region.nbytes) would be
            # accounting for ~1/10th of the memory actually retained.
            region = np.ascontiguousarray(crop_chat_region(frame, *args.chat_region))
            del frame
            if sem is None:
                ram_cap_bytes = (getattr(args, "ram_cap_gb", None) or 10) * 1024 ** 3
                try:
                    import psutil
                    available_bytes = psutil.virtual_memory().available
                    ram_cap_bytes = min(ram_cap_bytes, int(available_bytes * 0.5))
                except ImportError:
                    pass
                max_in_flight = max(workers * 2, int(ram_cap_bytes // region.nbytes))
                print(
                    f"  [info] OCR concurrency cap: {max_in_flight} frame(s) in flight "
                    f"(~{max_in_flight * region.nbytes / 1024 ** 3:.2f} GB)"
                )
                sem = threading.Semaphore(max_in_flight)
            sem.acquire()
            executor.submit(run_ocr_on_region, region).add_done_callback(_make_callback(second))
    except BaseException:
        # Cancel (or any other failure) mid-extraction: drop queued work
        # instead of waiting for it to drain, so Cancel is actually fast.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    elapsed = time.monotonic() - start_time
    frame_texts = sorted(results.items())

    if not args.verbose:
        sys.stdout.write("\n")
    else:
        for second, text in frame_texts:
            print(f"  [{second}s] OCR: {text[:100].replace(chr(10), ' ')}")

    rate = len(frame_texts) / elapsed if elapsed > 0 else 0
    print(f"  Done. {len(frame_texts)} frames in {_fmt_duration(elapsed)} ({rate:.1f} fps)")
    if failed_count[0]:
        print(f"  Warning: {failed_count[0]} frame(s) failed OCR and were skipped", file=sys.stderr)

    _check_cancel(args)

    # Step 3: Detect CD/WF and pair them
    _notify("Analyzing chat for CD/WF commands...")
    print("\n[2/4] Analyzing chat for CD/WF commands...")
    _tourn = getattr(args, "tournament_match", False)
    _detect_countdown = getattr(args, "detect_countdown", True)
    if _detect_countdown and not _tourn:
        print(
            "  [info] Countdown detection is enabled: a bare descending countdown "
            "('10', '9', '8', ...) is treated as an implicit CD. In OCR mode this "
            "can produce spurious clip starts from misread digits — disable it in "
            "Advanced options if clips look wrong."
        )
    cd_times, wf_times = analyze_frames(frame_texts, verbose=args.verbose,
                                         tournament_mode=_tourn,
                                         detect_countdown=_detect_countdown)
    print(f"  Found {len(cd_times)} CD(s) at: {cd_times}")
    print(f"  Found {len(wf_times)} WF(s) at: {wf_times}")

    pairs = pair_cd_wf(cd_times, wf_times)
    print(f"  Paired {len(pairs)} clip(s): {pairs}")

    _warn_orphans(cd_times, wf_times, pairs)

    start_buf, end_buf = _buffers_for(args)
    if start_buf or end_buf:
        pairs = apply_buffers(pairs, start_buf, end_buf, duration)
        print(f"  Applied buffers (start={start_buf:+d}s, end={end_buf:+d}s): {pairs}")

    if not pairs:
        print("\nNo CD→WF pairs found. Nothing to clip.", file=sys.stderr)
        sys.exit(0)

    # Steps 4 & 5: Extract and stitch clips (or raw-mode chapters only)
    return _produce_output(args, pairs, duration, output_dir, _notify, first_step=3)


def main():
    args = parse_args()
    try:
        run(args)
    except MissingDependencyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
