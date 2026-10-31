"""
chat_log_parser.py - Parse EVE Online local-chat log files to find CD/WF commands.

End markers are WF or GF.  A bare time-up call ("time", "time's up",
"timeout") also ends a round, but only when no WF/GF follows it before the
next round starts.

EVE exports chat logs as UTF-16 LE files.  Each message line has the form:
    [ YYYY.MM.DD HH:MM:SS ] PlayerName > message text

Multiple log files may cover different (non-overlapping) portions of the same
session; pass them all in together.

Usage:
    cd_times, wf_times = parse_chat_logs(
        log_paths,
        t0_seconds,   # EVE game-time seconds at video second 0
        duration,     # total video duration in seconds
    )
"""

import re
from datetime import datetime, timezone
from typing import List, Tuple, Optional


# Match: [ 2026.03.25 01:07:49 ] PlayerName > message
_LOG_LINE_RE = re.compile(
    r'\[\s*(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s+(.+?)\s+>\s+(.+)'
)

_FIRST_WORD_CD = re.compile(r'^\s*CD\b', re.IGNORECASE)
_FIRST_WORD_WF = re.compile(r'^\s*WF\b', re.IGNORECASE)

# A round that goes to the time limit often has no WF/GF; the only end signal
# is someone calling "time".  Match the whole message only, so chat like
# "time to go" or "4.5 mins left" is not treated as an end marker.
# People often type "time" a few seconds before "gf"/"wf", so parse_chat_logs
# only uses a time-up call when no WF/GF follows it before the next round.
_TIME_UP_RE = re.compile(
    r"^[^a-z0-9]*(time|time'?s up|time ?out)[^a-z0-9]*$", re.IGNORECASE
)

# --- Bare-countdown detection -------------------------------------------
# Some callers skip the "CD" announcement and go straight to counting down
# ("10", "9", "8", ...).  Without an explicit CD, parse_chat_logs has no
# start marker and pair_cd_wf silently drops the round's WF as an orphan.
# find_countdown_starts() recovers a synthetic CD from these bare-number
# runs so those rounds still get clipped.

# A countdown run must have at least this many messages to count as one
# (rules out a single stray number like "1|2|3|4" or a one-off "5").
_MIN_COUNTDOWN_LEN = 4
# Consecutive countdown messages more than this many seconds apart are not
# part of the same run (real countdowns tick roughly once a second).
_MAX_COUNTDOWN_GAP_SEC = 6
# The run's first number must be at least this high, so a countdown that's
# already underway when the log starts doesn't require the very first "10".
_MIN_COUNTDOWN_START = 8
# Suppress a synthetic countdown-CD if an explicit CD/COUNTDOWN message
# appears within this many seconds of it (the caller announced "CD" and
# then counted down — the explicit CD already marks the start).
_COUNTDOWN_CD_DEDUPE = 30


def find_countdown_starts(
    entries: List[Tuple[datetime, str, str]],
) -> List[datetime]:
    """
    Scan chronologically-sorted (timestamp, player, message) entries for
    bare descending-number countdown runs ("10", "9", "8", ...) and return
    the timestamp of the first message in each qualifying run.

    A run qualifies when it has >= _MIN_COUNTDOWN_LEN messages from the same
    player, each a bare integer, strictly descending (a single missed
    number is tolerated), each <= _MAX_COUNTDOWN_GAP_SEC seconds after the
    previous, and starting at >= _MIN_COUNTDOWN_START.
    """
    starts: List[datetime] = []
    run: List[Tuple[datetime, str, int]] = []  # (ts, player, value)

    def _flush():
        if len(run) >= _MIN_COUNTDOWN_LEN:
            starts.append(run[0][0])
        run.clear()

    for ts, player, msg in entries:
        word = msg.strip()
        value: Optional[int] = None
        if re.fullmatch(r'\d{1,2}', word):
            n = int(word)
            if 0 <= n <= 10:
                value = n

        if value is None:
            _flush()
            continue

        if run:
            prev_ts, prev_player, prev_value = run[-1]
            gap = (ts - prev_ts).total_seconds()
            descending = 0 < prev_value - value <= 2
            if player != prev_player or gap > _MAX_COUNTDOWN_GAP_SEC or gap < 0 or not descending:
                _flush()

        if not run and value < _MIN_COUNTDOWN_START:
            continue  # can't start a fresh run too far into the countdown

        run.append((ts, player, value))

    _flush()
    return starts


def _parse_log_line(line: str) -> Optional[Tuple[datetime, str, str]]:
    """Parse a single chat log line; return (timestamp, player, message) or None."""
    line = line.strip().lstrip('\ufeff')
    m = _LOG_LINE_RE.match(line)
    if not m:
        return None
    ts_str, player, message = m.group(1), m.group(2).strip(), m.group(3).strip()
    try:
        ts = datetime.strptime(ts_str.strip(), '%Y.%m.%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return ts, player, message


def read_chat_log(path: str) -> List[Tuple[datetime, str, str]]:
    """Read one EVE chat log file; return list of (timestamp, player, message)."""
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        text = raw.decode('utf-16')
    except UnicodeDecodeError:
        text = raw.decode('utf-16-le', errors='replace')
    entries = []
    for line in text.split('\n'):
        parsed = _parse_log_line(line)
        if parsed:
            entries.append(parsed)
    return entries


def _to_video_seconds(ts: datetime, t0_game_seconds: float, video_duration: float) -> Optional[int]:
    """
    Convert a game timestamp to a video second, or None if it falls outside
    [0, video_duration].  Handles the midnight-wrap case where the log
    crosses UTC midnight relative to t0.
    """
    game_sec = ts.hour * 3600 + ts.minute * 60 + ts.second
    video_sec = game_sec - t0_game_seconds

    if video_sec < -3600:
        video_sec += 86400  # add 24 hours

    if video_sec < 0 or video_sec > video_duration:
        return None
    return int(video_sec)


def parse_chat_logs(
    log_paths: List[str],
    t0_game_seconds: float,
    video_duration: float,
    tournament_mode: bool = False,
    detect_countdown: bool = True,
) -> Tuple[List[int], List[int]]:
    """
    Extract CD and WF video timestamps from EVE chat log files.

    WF timestamps include GF.  They also include a bare time-up call
    ("time", "time's up", "timeout") when no WF/GF follows it before the next
    CD, so a round that ends on the time limit is still clipped.  Only the
    first time-up call of a round is used.

    Args:
        log_paths: Paths to one or more EVE chat log files (any order; will be
            merged and sorted chronologically).
        t0_game_seconds: EVE game time (seconds since midnight UTC on the log
            date) at video second 0.  Compute as:
                t0_game_seconds = hh * 3600 + mm * 60 + ss
            where HH:MM:SS is the EVE game time when video recording began.
        video_duration: Total video length in seconds; events outside
            [0, duration] are discarded.
        tournament_mode: When True, detect tournament system messages instead of
            CD/WF player commands.  Start marker: EVE System message containing
            "30 seconds until match start".  End marker: EVE System message
            containing "Match completed!".
        detect_countdown: When True (and not tournament_mode), also treat a
            bare descending countdown ("10", "9", "8", ...) as an implicit CD
            for rounds where no "CD" was typed.  See find_countdown_starts().

    Returns:
        (cd_timestamps, wf_timestamps): lists of integer video seconds.
    """
    # Collect all entries from all files, deduplicate by (timestamp, player,
    # message) in case files overlap slightly.
    all_entries: dict = {}
    for path in log_paths:
        for ts, player, msg in read_chat_log(path):
            key = (ts, player, msg)
            all_entries[key] = (ts, player, msg)

    sorted_entries = sorted(all_entries.values(), key=lambda x: x[0])

    cd_timestamps: List[int] = []
    wf_timestamps: List[int] = []
    explicit_cd_ts: List[datetime] = []
    explicit_wf_ts: List[datetime] = []
    time_up_ts: List[datetime] = []

    for ts, player, msg in sorted_entries:
        if tournament_mode:
            is_cd = player.strip() == "EVE System" and "30 seconds until match start" in msg
            is_wf = player.strip() == "EVE System" and "Match completed!" in msg
        else:
            words = msg.strip().split()
            if not words:
                continue
            # Strip surrounding non-alphanumeric characters before classifying
            # (e.g. "-CD-" → "CD", "CD__________" → "CD").
            # \W misses underscore; use [^a-zA-Z0-9] instead.
            # Skip tokens that are pure punctuation (e.g. "*****" in "***** CD ******")
            # and use the first token that has at least one alphanumeric character.
            first = ''
            for w in words:
                stripped = re.sub(r'^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$', '', w).upper()
                if stripped:
                    first = stripped
                    break
            is_cd = first in ('CD', 'COUNTDOWN')
            is_wf = first in ('WF', 'GF')
            if _TIME_UP_RE.match(msg.strip()):
                time_up_ts.append(ts)
                continue
        if not is_cd and not is_wf:
            continue

        if is_cd:
            explicit_cd_ts.append(ts)
        else:
            explicit_wf_ts.append(ts)

        video_sec = _to_video_seconds(ts, t0_game_seconds, video_duration)
        if video_sec is None:
            continue

        if is_cd:
            cd_timestamps.append(video_sec)
        else:
            wf_timestamps.append(video_sec)

    countdown_starts: List[datetime] = []
    if detect_countdown and not tournament_mode:
        countdown_starts = find_countdown_starts(sorted_entries)

    # Use a time-up call as an end marker only when the next event after it is
    # a round start (or nothing), i.e. the round had no WF/GF of its own.
    round_starts = sorted(explicit_cd_ts + countdown_starts)
    last_time_up: Optional[datetime] = None
    for t in time_up_ts:
        next_wf = next((w for w in explicit_wf_ts if w >= t), None)
        next_cd = next((c for c in round_starts if c > t), None)
        if next_wf is not None and (next_cd is None or next_wf < next_cd):
            continue  # a real WF/GF ends this round
        if last_time_up is not None and not any(last_time_up < c < t for c in round_starts):
            continue  # already have a time-up call for this round
        last_time_up = t
        video_sec = _to_video_seconds(t, t0_game_seconds, video_duration)
        if video_sec is not None:
            wf_timestamps.append(video_sec)
    wf_timestamps.sort()

    if detect_countdown and not tournament_mode:
        for start_ts in countdown_starts:
            # Skip a synthetic CD if an explicit CD/COUNTDOWN was already
            # typed near it — the round already has a real start marker.
            if any(abs((start_ts - cd_ts).total_seconds()) <= _COUNTDOWN_CD_DEDUPE
                   for cd_ts in explicit_cd_ts):
                continue
            video_sec = _to_video_seconds(start_ts, t0_game_seconds, video_duration)
            if video_sec is not None:
                cd_timestamps.append(video_sec)
        cd_timestamps = sorted(set(cd_timestamps))

    return cd_timestamps, wf_timestamps


def game_time_to_seconds(time_str: str) -> int:
    """
    Convert an EVE game time string to seconds-since-midnight.

    Accepted formats: "HH:MM:SS" or "HH:MM".
    """
    parts = time_str.strip().split(':')
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    else:
        raise ValueError(f"Invalid game time format: {time_str!r}")
    return h * 3600 + m * 60 + s
