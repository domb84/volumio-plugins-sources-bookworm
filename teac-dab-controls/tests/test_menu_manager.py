"""Tests for includes/menu_manager.py: the restart-marker gate."""
import os
import time

from includes import menu_manager as mm


class TestConsumeRestartMarker:
    def test_absent_marker_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm, "_RESTART_MARKER_PATH", str(tmp_path / "marker"))
        assert mm.MenuManager._consume_restart_marker() is False

    def test_fresh_marker_returns_true_and_is_consumed(self, tmp_path, monkeypatch):
        marker = tmp_path / "marker"
        marker.write_text("x")
        monkeypatch.setattr(mm, "_RESTART_MARKER_PATH", str(marker))
        assert mm.MenuManager._consume_restart_marker() is True
        assert not marker.exists()

    def test_stale_marker_returns_false_but_is_still_removed(self, tmp_path, monkeypatch):
        marker = tmp_path / "marker"
        marker.write_text("x")
        old = time.time() - 60  # older than the 30s freshness window
        os.utime(marker, (old, old))
        monkeypatch.setattr(mm, "_RESTART_MARKER_PATH", str(marker))
        assert mm.MenuManager._consume_restart_marker() is False
        assert not marker.exists()
