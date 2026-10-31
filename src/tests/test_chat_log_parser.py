"""
Tests for chat_log_parser.py
"""

import sys
import os
import tempfile

from chat_log_parser import (
    read_chat_log, parse_chat_logs, game_time_to_seconds, _parse_log_line,
    find_countdown_starts,
)


# ---------------------------------------------------------------------------
# game_time_to_seconds
# ---------------------------------------------------------------------------

class TestGameTimeToSeconds:
    def test_hh_mm_ss(self):
        assert game_time_to_seconds("01:07:49") == 1 * 3600 + 7 * 60 + 49

    def test_hh_mm(self):
        assert game_time_to_seconds("01:07") == 1 * 3600 + 7 * 60

    def test_midnight(self):
        assert game_time_to_seconds("00:00:00") == 0

    def test_end_of_day(self):
        assert game_time_to_seconds("23:59:59") == 23 * 3600 + 59 * 60 + 59


# ---------------------------------------------------------------------------
# read_chat_log
# ---------------------------------------------------------------------------

def _write_log(content: str, encoding: str = 'utf-16') -> str:
    """Write content as a UTF-16 chat log to a temp file; return the path."""
    fd, path = tempfile.mkstemp(suffix='.txt')
    with os.fdopen(fd, 'wb') as f:
        f.write(content.encode(encoding))
    return path


_SAMPLE_LOG = """\
\ufeff
 - - - - - - - - -
 Channel ID:      local
 Channel Name:    Local
 Listener:        Test Player
 Session started: 2026.03.25 01:00:00
 - - - - - - - - -

\ufeff[ 2026.03.25 01:07:49 ] Ceofore Aideron > CD
\ufeff[ 2026.03.25 01:14:56 ] Ceofore Aideron > CD
\ufeff[ 2026.03.25 01:17:39 ] Arkadiy Krylov > 3brand wf
\ufeff[ 2026.03.25 01:19:50 ] Rima Ambraelle > wf
\ufeff[ 2026.03.25 01:20:00 ] Some Player > ok WF
"""


class TestReadChatLog:
    def test_reads_messages(self):
        path = _write_log(_SAMPLE_LOG)
        try:
            entries = read_chat_log(path)
            assert len(entries) == 5
        finally:
            os.unlink(path)

    def test_correct_timestamp(self):
        path = _write_log(_SAMPLE_LOG)
        try:
            entries = read_chat_log(path)
            ts, player, msg = entries[0]
            assert ts.hour == 1 and ts.minute == 7 and ts.second == 49
            assert player == "Ceofore Aideron"
            assert msg == "CD"
        finally:
            os.unlink(path)

    def test_skips_non_message_lines(self):
        path = _write_log(_SAMPLE_LOG)
        try:
            entries = read_chat_log(path)
            # Only lines matching [ ts ] player > msg should be returned
            players = [e[1] for e in entries]
            assert "Channel ID" not in players
            assert "Session started" not in players
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# parse_chat_logs
# ---------------------------------------------------------------------------

class TestParseChatLogs:
    def _make_log(self, messages):
        """messages: list of (HH:MM:SS, player, text)"""
        lines = ["\ufeff\n Session started: 2026.03.25 00:00:00\n"]
        for ts, player, msg in messages:
            lines.append(f"\ufeff[ 2026.03.25 {ts} ] {player} > {msg}")
        path = _write_log("\n".join(lines))
        return path

    def test_cd_and_wf_detected(self):
        path = self._make_log([
            ("01:07:49", "Ceofore", "CD"),
            ("01:14:56", "Ceofore", "CD"),
            ("01:19:50", "Rima", "wf"),
        ])
        # t0 = 01:05:00 = 3900s; video 0-3600s
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 3600)
            # CD at 01:07:49 → video t = 1:07:49 - 1:05:00 = 2:49 = 169s
            # CD at 01:14:56 → video t = 9:56 = 596s
            # WF at 01:19:50 → video t = 14:50 = 890s
            assert 169 in cd_times
            assert 596 in cd_times
            assert 890 in wf_times
        finally:
            os.unlink(path)

    def test_events_outside_duration_excluded(self):
        path = self._make_log([
            ("01:07:49", "P", "CD"),   # t=169s
            ("01:30:00", "P", "WF"),   # t=1500s → beyond 600s duration
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 600)
            assert 169 in cd_times
            assert len(wf_times) == 0  # WF at t=1500 is out of range
        finally:
            os.unlink(path)

    def test_events_before_t0_excluded(self):
        path = self._make_log([
            ("01:00:00", "P", "CD"),   # before T0
            ("01:07:49", "P", "WF"),   # after T0 → t=169s
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 3600)
            assert len(cd_times) == 0
            assert 169 in wf_times
        finally:
            os.unlink(path)

    def test_ok_wf_not_detected(self):
        # "ok WF" must not be counted — WF is not the first word
        path = self._make_log([
            ("01:07:49", "P", "CD"),
            ("01:14:56", "P", "ok WF"),
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 3600)
            assert len(wf_times) == 0
        finally:
            os.unlink(path)

    def test_multi_file_merge(self):
        # Two non-overlapping log files covering different periods
        path1 = self._make_log([
            ("01:07:49", "Ceofore", "CD"),
            ("01:14:56", "Ceofore", "CD"),
        ])
        path2 = self._make_log([
            ("01:19:50", "Rima", "wf"),
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path1, path2], t0, 3600)
            assert len(cd_times) == 2
            assert len(wf_times) == 1
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_cd_cd_cd_counts_once(self):
        # "CD CD CD" message — only first word is CD, so counts as 1 CD
        path = self._make_log([
            ("02:22:10", "Arkadiy", "CD CD CD"),
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 7200)
            # Should produce 1 CD, not 3
            assert len(cd_times) == 1
        finally:
            os.unlink(path)

    def test_duplicate_entries_across_overlapping_files_deduplicated(self):
        # Both files contain the same message (overlapping export windows).
        # The entry must appear only once in the output.
        path1 = self._make_log([
            ("01:07:49", "Ceofore", "CD"),
            ("01:19:50", "Rima", "wf"),
        ])
        path2 = self._make_log([
            ("01:07:49", "Ceofore", "CD"),   # duplicate
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path1, path2], t0, 3600)
            assert len(cd_times) == 1
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_midnight_wrap_excluded(self):
        # video_sec = game_sec - t0; if that result is < -3600 the code adds
        # 86400 (midnight crossing).  A message whose adjusted video_sec still
        # falls outside [0, duration] must be excluded.
        # Simulate: t0=1*3600=3600 (01:00:00), event at 00:30:00 → video_sec=-1800
        # -1800 > -3600 so no wrap is applied → excluded (video_sec < 0)
        path = self._make_log([
            ("00:30:00", "P", "CD"),  # before t0, no wrap applied
        ])
        t0 = game_time_to_seconds("01:00:00")
        try:
            cd_times, _ = parse_chat_logs([path], t0, 3600)
            assert len(cd_times) == 0
        finally:
            os.unlink(path)

    def test_empty_message_skipped(self):
        # Entries with an empty message must be silently skipped
        # (the "if not words: continue" path, line 98-99).
        # read_chat_log is mocked to inject an entry with msg="" directly,
        # since _parse_log_line strips trailing whitespace before regex matching.
        from unittest.mock import patch
        from datetime import datetime, timezone
        ts_cd = datetime(2026, 3, 25, 1, 7, 49, tzinfo=timezone.utc)
        ts_empty = datetime(2026, 3, 25, 1, 8, 0, tzinfo=timezone.utc)
        fake_entries = [(ts_cd, "P", "CD"), (ts_empty, "P", "")]
        t0 = game_time_to_seconds("01:05:00")
        with patch("chat_log_parser.read_chat_log", return_value=fake_entries):
            cd_times, wf_times = parse_chat_logs(["fake.log"], t0, 3600)
        assert len(cd_times) == 1   # only the real CD is counted

    def test_cd_with_trailing_underscores_detected(self):
        # "CD__________" must be classified as CD — trailing underscores were
        # previously not stripped because \W excludes '_' (a word character).
        path = self._make_log([
            ("01:07:49", "Stu", "CD__________"),
            ("01:19:50", "Rima", "wf"),
        ])
        t0 = game_time_to_seconds("01:05:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 3600)
            assert 169 in cd_times
            assert 890 in wf_times
        finally:
            os.unlink(path)

    def test_midnight_wrap_applied_and_included(self):
        # Midnight crossing: t0=23:30:00 (84600s), event at 00:30:00 (1800s).
        # video_sec = 1800 - 84600 = -82800 < -3600 → wrap: +86400 → 3600.
        # Within duration 7200 → event is included.
        path = self._make_log([
            ("00:30:00", "P", "CD"),
        ])
        t0 = game_time_to_seconds("23:30:00")
        try:
            cd_times, _ = parse_chat_logs([path], t0, 7200)
            assert 3600 in cd_times
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# game_time_to_seconds — error handling
# ---------------------------------------------------------------------------

class TestGameTimeToSecondsErrors:
    def test_invalid_format_raises_value_error(self):
        import pytest
        with pytest.raises(ValueError, match="Invalid game time"):
            game_time_to_seconds("01:07:49:00")

    def test_single_component_raises_value_error(self):
        import pytest
        with pytest.raises(ValueError):
            game_time_to_seconds("3600")


# ---------------------------------------------------------------------------
# _parse_log_line
# ---------------------------------------------------------------------------

class TestParseLogLine:
    def test_valid_line_returns_tuple(self):
        line = "[ 2026.03.25 01:07:49 ] Ceofore Aideron > CD"
        result = _parse_log_line(line)
        assert result is not None
        ts, player, msg = result
        assert ts.hour == 1 and ts.minute == 7 and ts.second == 49
        assert player == "Ceofore Aideron"
        assert msg == "CD"

    def test_non_message_line_returns_none(self):
        assert _parse_log_line(" Channel ID:      local") is None
        assert _parse_log_line("") is None
        assert _parse_log_line(" - - - - - - - - -") is None

    def test_bom_stripped(self):
        # Lines in UTF-16 logs often start with a BOM character (\ufeff)
        line = "\ufeff[ 2026.03.25 01:07:49 ] Player > hello"
        result = _parse_log_line(line)
        assert result is not None
        _, player, msg = result
        assert player == "Player"
        assert msg == "hello"

    def test_whitespace_trimmed_from_player_and_message(self):
        line = "[ 2026.03.25 01:07:49 ]  Padded Player  >  trimmed message  "
        result = _parse_log_line(line)
        assert result is not None
        _, player, msg = result
        assert player == "Padded Player"
        assert msg == "trimmed message"

    def test_invalid_date_returns_none(self):
        # Month 13 matches the regex but fails strptime → ValueError → None
        line = "[ 2026.13.25 01:07:49 ] Player > CD"
        assert _parse_log_line(line) is None


# ---------------------------------------------------------------------------
# read_chat_log — UTF-16-LE fallback
# ---------------------------------------------------------------------------

class TestReadChatLogUtf16Fallback:
    def test_utf16_le_fallback_on_decode_error(self):
        # Odd-length byte string raises UnicodeDecodeError for 'utf-16';
        # the fallback 'utf-16-le' with errors='replace' must succeed.
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix='.txt')
        with os.fdopen(fd, 'wb') as f:
            f.write(b'\x58\x00\x59')  # 3 bytes: odd length → utf-16 fails
        try:
            entries = read_chat_log(path)
            assert isinstance(entries, list)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# find_countdown_starts / detect_countdown
#
# Some callers skip the "CD" announcement and go straight to counting down
# ("10", "9", "8", ...). Without a CD marker, parse_chat_logs would leave the
# round's WF orphaned and pair_cd_wf would silently drop it.
# ---------------------------------------------------------------------------

def _entries(messages):
    """messages: list of (HH:MM:SS, player, text) -> [(datetime, player, msg), ...]"""
    from datetime import datetime, timezone
    out = []
    for ts, player, msg in messages:
        h, m, s = (int(x) for x in ts.split(':'))
        out.append((datetime(2026, 3, 25, h, m, s, tzinfo=timezone.utc), player, msg))
    return out


def _countdown_messages(start_ts, player="Daed Alas", start=10, step_sec=1):
    """Build a clean descending countdown run's (ts, player, msg) tuples."""
    h, m, s = (int(x) for x in start_ts.split(':'))
    base = h * 3600 + m * 60 + s
    out = []
    for i, n in enumerate(range(start, -1, -1)):
        secs = base + i * step_sec
        hh, mm, ss = secs // 3600, (secs % 3600) // 60, secs % 60
        out.append((f"{hh:02d}:{mm:02d}:{ss:02d}", player, str(n)))
    return out


class TestFindCountdownStarts:
    def test_clean_run_detected(self):
        entries = _entries(_countdown_messages("01:31:24"))
        starts = find_countdown_starts(entries)
        assert len(starts) == 1
        assert starts[0].hour == 1 and starts[0].minute == 31 and starts[0].second == 24

    def test_run_with_one_missed_number(self):
        # 10, 9, 7, 6, ... (8 skipped) — still descending by <= 2, still counts
        msgs = [m for m in _countdown_messages("01:31:24") if m[2] != "8"]
        starts = find_countdown_starts(_entries(msgs))
        assert len(starts) == 1

    def test_short_run_rejected(self):
        # Only 3 messages ("9","8","7") — below _MIN_COUNTDOWN_LEN, even
        # though the start value (9) clears _MIN_COUNTDOWN_START.
        msgs = [("01:31:24", "P", "9"), ("01:31:25", "P", "8"), ("01:31:26", "P", "7")]
        starts = find_countdown_starts(_entries(msgs))
        assert starts == []

    def test_ascending_sequence_rejected(self):
        msgs = [("01:00:00", "P", "1"), ("01:00:01", "P", "2"),
                ("01:00:02", "P", "3"), ("01:00:03", "P", "4")]
        starts = find_countdown_starts(_entries(msgs))
        assert starts == []

    def test_pipe_separated_numbers_not_a_run(self):
        # "1|2|3|4" is a single message, not four separate ones
        msgs = [("01:00:00", "P", "1|2|3|4")]
        starts = find_countdown_starts(_entries(msgs))
        assert starts == []

    def test_numbers_split_across_two_speakers_rejected(self):
        msgs = _countdown_messages("01:31:24", player="A")
        # Interleave a second speaker's numbers so no single speaker has a run
        msgs = [msgs[0], ("01:31:25", "B", "9"), msgs[2], ("01:31:27", "B", "7")]
        starts = find_countdown_starts(_entries(msgs))
        assert starts == []

    def test_gap_too_large_splits_run(self):
        # First run: 10, 9, 8 (only 3 messages — too short to count).
        # A 30s gap (past _MAX_COUNTDOWN_GAP_SEC) separates it from a second,
        # unrelated run: 7, 6, 5, 4, 3, 2, 1, 0 (8 messages — long enough).
        first = [("01:31:24", "P", "10"), ("01:31:25", "P", "9"), ("01:31:26", "P", "8")]
        second = _countdown_messages("01:32:00", start=8)  # "01:32:00" .. "01:32:08"
        starts = find_countdown_starts(_entries(first + second))
        assert len(starts) == 1
        assert starts[0].minute == 32 and starts[0].second == 0


class TestParseChatLogsCountdownIntegration:
    def _make_log(self, messages):
        lines = ["﻿\n Session started: 2026.03.25 00:00:00\n"]
        for ts, player, msg in messages:
            lines.append(f"﻿[ 2026.03.25 {ts} ] {player} > {msg}")
        return _write_log("\n".join(lines))

    def test_bare_countdown_becomes_cd(self):
        messages = _countdown_messages("01:31:24") + [("01:36:41", "Aaron", "wf")]
        path = self._make_log(messages)
        t0 = game_time_to_seconds("01:00:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 7200)
            assert len(cd_times) == 1
            assert len(wf_times) == 1
            # CD at 01:31:24 -> video t = 31:24 = 1884s
            assert cd_times[0] == 31 * 60 + 24
        finally:
            os.unlink(path)

    def test_disabled_flag_restores_old_behavior(self):
        messages = _countdown_messages("01:31:24") + [("01:36:41", "Aaron", "wf")]
        path = self._make_log(messages)
        t0 = game_time_to_seconds("01:00:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 7200, detect_countdown=False)
            assert len(cd_times) == 0
            assert len(wf_times) == 1
        finally:
            os.unlink(path)

    def test_explicit_cd_suppresses_synthetic_countdown_cd(self):
        # Caller announces "CD" and then counts down — must produce exactly
        # one CD, not two.
        messages = [("01:31:20", "Syss7", "== CD ==")] + _countdown_messages("01:31:24") \
            + [("01:36:41", "Aaron", "wf")]
        path = self._make_log(messages)
        t0 = game_time_to_seconds("01:00:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 7200)
            assert len(cd_times) == 1
            assert cd_times[0] == 31 * 60 + 20  # the explicit CD wins
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Time-up end marker
# ---------------------------------------------------------------------------
# A round that goes to the time limit often has no WF/GF; the only end signal
# is someone typing "time".  Without it the round's CD is orphaned and the
# match is silently dropped (seen 2026-10-30: 8 matches played, 7 clipped).

class TestTimeUpEndMarker:
    def _make_log(self, messages):
        """messages: list of (HH:MM:SS, player, text)"""
        lines = ["﻿\n Session started: 2026.10.30 13:00:00\n"]
        for ts, player, msg in messages:
            lines.append(f"﻿[ 2026.10.30 {ts} ] {player} > {msg}")
        return _write_log("\n".join(lines))

    def _end_markers(self, msg):
        path = self._make_log([
            ("13:59:06", "Daed Alas", "CD"),
            ("14:09:23", "Stalence", msg),
        ])
        t0 = game_time_to_seconds("13:00:00")
        try:
            _, wf_times = parse_chat_logs([path], t0, 7200)
            return wf_times
        finally:
            os.unlink(path)

    def test_time_detected(self):
        assert self._end_markers("time") == [69 * 60 + 23]

    def test_variants_detected(self):
        for msg in ("TIME", "time's up", "times up", "timeout", "time out", "time!"):
            assert self._end_markers(msg) == [69 * 60 + 23], msg

    def test_time_in_sentence_not_detected(self):
        for msg in ("time to go", "4.5 mins left", "ok time", "timer?"):
            assert self._end_markers(msg) == [], msg

    def test_time_before_gf_ignored(self):
        # "time" a few seconds before "gf" must not end the clip early
        path = self._make_log([
            ("13:59:06", "Daed Alas", "CD"),
            ("14:09:23", "Stalence", "time"),
            ("14:09:26", "Stalence", "gf"),
        ])
        t0 = game_time_to_seconds("13:00:00")
        try:
            _, wf_times = parse_chat_logs([path], t0, 7200)
            assert wf_times == [69 * 60 + 26]
        finally:
            os.unlink(path)

    def test_time_same_second_as_gf_ignored(self):
        path = self._make_log([
            ("13:59:06", "Daed Alas", "CD"),
            ("14:09:23", "Stalence", "time!"),
            ("14:09:23", "Stalence", "gf"),
        ])
        t0 = game_time_to_seconds("13:00:00")
        try:
            _, wf_times = parse_chat_logs([path], t0, 7200)
            assert wf_times == [69 * 60 + 23]
        finally:
            os.unlink(path)

    def test_repeated_time_calls_count_once(self):
        path = self._make_log([
            ("13:59:06", "Daed Alas", "CD"),
            ("14:09:23", "Stalence", "time"),
            ("14:09:24", "Daed Alas", "time"),
        ])
        t0 = game_time_to_seconds("13:00:00")
        try:
            _, wf_times = parse_chat_logs([path], t0, 7200)
            assert wf_times == [69 * 60 + 23]
        finally:
            os.unlink(path)

    def test_time_before_bare_countdown_round_kept(self):
        # Next round starts with a bare countdown (no "CD"), then "wf".
        path = self._make_log([
            ("13:59:06", "Daed Alas", "CD"),
            ("14:09:23", "Stalence", "time"),
        ] + [(f"14:20:{25 + i:02d}", "Daed Alas", str(10 - i)) for i in range(11)] + [
            ("14:25:10", "Rima Ambraelle", "wf"),
        ])
        t0 = game_time_to_seconds("13:00:00")
        try:
            _, wf_times = parse_chat_logs([path], t0, 7200)
            assert wf_times == [69 * 60 + 23, 85 * 60 + 10]
        finally:
            os.unlink(path)

    def test_timed_out_round_paired(self):
        from chat_analyzer import pair_cd_wf
        path = self._make_log([
            ("13:59:06", "Daed Alas", "CD"),
            ("13:59:08", "Daed Alas", "10"),
            ("13:59:09", "Daed Alas", "9"),
            ("13:59:10", "Daed Alas", "8"),
            ("13:59:11", "Daed Alas", "7"),
            ("14:04:52", "Stalence", "4.5 mins left"),
            ("14:09:20", "Daed Alas", "0"),
            ("14:09:23", "Stalence", "time"),
            ("14:20:23", "Daed Alas", "CD"),
            ("14:25:10", "Rima Ambraelle", "wf"),
        ])
        t0 = game_time_to_seconds("13:00:00")
        try:
            cd_times, wf_times = parse_chat_logs([path], t0, 7200)
            pairs = pair_cd_wf(cd_times, wf_times)
            assert pairs == [
                (59 * 60 + 6, 69 * 60 + 23),
                (80 * 60 + 23, 85 * 60 + 10),
            ]
        finally:
            os.unlink(path)
