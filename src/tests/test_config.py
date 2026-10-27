"""
Tests for config.py — load_config and save_config.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import config as cfg


class TestMostRecentFileMtime:
    def test_returns_negative_one_on_oserror(self):
        p = MagicMock(spec=Path)
        p.iterdir.side_effect = OSError("no access")
        result = cfg._most_recent_file_mtime(p)
        assert result == -1.0

    def test_returns_negative_one_for_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = cfg._most_recent_file_mtime(Path(tmpdir))
        assert result == -1.0

    def test_returns_max_mtime_for_directory_with_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            f1 = d / "a.txt"
            f2 = d / "b.txt"
            f1.write_text("a")
            time.sleep(0.01)
            f2.write_text("b")
            expected_mtime = f2.stat().st_mtime
            result = cfg._most_recent_file_mtime(d)
        assert result == expected_mtime

    def test_ignores_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            subdir = d / "subdir"
            subdir.mkdir()
            f = d / "file.txt"
            f.write_text("x")
            expected_mtime = f.stat().st_mtime
            result = cfg._most_recent_file_mtime(d)
        assert result == expected_mtime


class TestDefaultLogDir:
    def test_non_linux_returns_documents_path(self):
        with patch.object(cfg.sys, "platform", "darwin"):
            result = cfg.default_log_dir()
        expected = str(Path.home() / "Documents" / "EVE" / "logs" / "Chatlogs")
        assert result == expected

    def test_linux_no_candidates_returns_steam_path(self):
        with patch.object(cfg.sys, "platform", "linux"), \
             patch("os.getlogin", return_value="testuser"), \
             patch.object(Path, "is_dir", return_value=False):
            result = cfg.default_log_dir()
        steam_path = (
            Path.home()
            / ".local/share/Steam/steamapps/compatdata/8500/pfx"
            / "drive_c/users/steamuser/Documents/EVE/logs/Chatlogs"
        )
        assert result == str(steam_path)

    def test_linux_only_steam_candidate(self):
        steam_path = (
            Path.home()
            / ".local/share/Steam/steamapps/compatdata/8500/pfx"
            / "drive_c/users/steamuser/Documents/EVE/logs/Chatlogs"
        )

        def is_dir_side_effect(self):
            return self == steam_path

        with patch.object(cfg.sys, "platform", "linux"), \
             patch.dict("os.environ", {"USER": "testuser"}), \
             patch.object(Path, "is_dir", is_dir_side_effect):
            result = cfg.default_log_dir()
        assert result == str(steam_path)

    def test_linux_only_lutris_candidate(self):
        lutris_path = (
            Path.home()
            / "Games/eve-online/drive_c/users"
            / "testuser"
            / "Documents/EVE/logs/Chatlogs"
        )

        def is_dir_side_effect(self):
            return self == lutris_path

        with patch.object(cfg.sys, "platform", "linux"), \
             patch.dict("os.environ", {"USER": "testuser"}), \
             patch.object(Path, "is_dir", is_dir_side_effect):
            result = cfg.default_log_dir()
        assert result == str(lutris_path)

    def test_linux_both_candidates_returns_most_recent(self):
        steam_path = (
            Path.home()
            / ".local/share/Steam/steamapps/compatdata/8500/pfx"
            / "drive_c/users/steamuser/Documents/EVE/logs/Chatlogs"
        )
        lutris_path = (
            Path.home()
            / "Games/eve-online/drive_c/users"
            / "testuser"
            / "Documents/EVE/logs/Chatlogs"
        )

        def is_dir_side_effect(self):
            return self in (steam_path, lutris_path)

        # lutris has a more recent mtime
        def mtime_side_effect(p):
            return 200.0 if p == lutris_path else 100.0

        with patch.object(cfg.sys, "platform", "linux"), \
             patch.dict("os.environ", {"USER": "testuser"}), \
             patch.object(Path, "is_dir", is_dir_side_effect), \
             patch.object(cfg, "_most_recent_file_mtime", side_effect=mtime_side_effect):
            result = cfg.default_log_dir()
        assert result == str(lutris_path)


class TestLoadConfig:
    def test_returns_defaults_when_file_missing(self):
        nonexistent = Path(tempfile.mkdtemp()) / "nodir" / "config.json"
        with patch.object(cfg, "CONFIG_PATH", nonexistent):
            result = cfg.load_config()
        assert result == dict(cfg._DEFAULTS)

    def test_match_label_off_by_default(self):
        nonexistent = Path(tempfile.mkdtemp()) / "nodir" / "config.json"
        with patch.object(cfg, "CONFIG_PATH", nonexistent):
            result = cfg.load_config()
        assert result["match_label"] is False
        assert result["match_label_pos"] == [0.5, 0.05]

    def test_raw_mode_off_by_default(self):
        nonexistent = Path(tempfile.mkdtemp()) / "nodir" / "config.json"
        with patch.object(cfg, "CONFIG_PATH", nonexistent):
            result = cfg.load_config()
        assert result["raw_mode"] is False

    def test_file_values_override_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"video_dir": "/my/videos"}, f)
            path = Path(f.name)
        try:
            with patch.object(cfg, "CONFIG_PATH", path):
                result = cfg.load_config()
            assert result["video_dir"] == "/my/videos"
            assert result["log_dir"] == ""          # default filled in
            assert result["chat_region"] == [0.0, 0.35, 0.15, 1.0]
        finally:
            os.unlink(path)

    def test_full_config_loaded(self):
        data = {
            "video_dir": "/v",
            "log_dir": "/l",
            "output_dir": "/o",
            "chat_region": [0.1, 0.2, 0.3, 0.4],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            with patch.object(cfg, "CONFIG_PATH", path):
                result = cfg.load_config()
            expected = {**cfg._DEFAULTS, **data}
            assert result == expected
        finally:
            os.unlink(path)

    def test_corrupt_json_returns_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not valid json {{{")
            path = Path(f.name)
        try:
            with patch.object(cfg, "CONFIG_PATH", path):
                result = cfg.load_config()
            assert result == dict(cfg._DEFAULTS)
        finally:
            os.unlink(path)

    def test_extra_keys_in_file_preserved(self):
        data = {"video_dir": "/v", "custom_key": "value"}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            with patch.object(cfg, "CONFIG_PATH", path):
                result = cfg.load_config()
            assert result["custom_key"] == "value"
        finally:
            os.unlink(path)


class TestSaveConfig:
    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "config.json"
            with patch.object(cfg, "CONFIG_PATH", path):
                cfg.save_config({"video_dir": "/test"})
            assert path.exists()

    def test_writes_json_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            with patch.object(cfg, "CONFIG_PATH", path):
                cfg.save_config({"video_dir": "/test", "log_dir": "/logs"})
            data = json.loads(path.read_text())
            assert data["video_dir"] == "/test"
            assert data["log_dir"] == "/logs"

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            original = dict(cfg._DEFAULTS)
            original.update({"video_dir": "/v", "log_dir": "/l", "output_dir": "/o"})
            with patch.object(cfg, "CONFIG_PATH", path):
                cfg.save_config(original)
                result = cfg.load_config()
            assert result == original
