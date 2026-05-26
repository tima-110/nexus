"""CLI integration tests for Phase 4 commands: reconcile, install, uninstall, status, doctor."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from nexus.cli import app
from nexus.config import NexusConfig
from nexus.db import init_db

runner = CliRunner()


def _get_test_conn():
    """Create an in-memory connection with schema for CLI tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    # Insert a broker account
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance) VALUES (?, ?, ?)",
        ("paper1", 2.0, 100000.0),
    )
    conn.commit()
    return conn


class TestReconcileCommand:
    @patch("subprocess.run")
    @patch("nexus.db.get_connection")
    @patch("nexus.config.load_config")
    def test_reconcile_command_runs(self, mock_config, mock_conn, mock_subprocess):
        """reconcile command should run and show output."""
        mock_config.return_value = NexusConfig()
        conn = _get_test_conn()
        mock_conn.return_value = conn
        # Broker unreachable
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = runner.invoke(app, ["reconcile"])

        # Command runs (may exit with code 1 due to errors, but should not crash)
        assert result.exit_code in (0, 1)
        assert "Orders synced" in result.output or "Error" in result.output or "Orders" in result.output

    @patch("subprocess.run")
    @patch("nexus.db.get_connection")
    @patch("nexus.config.load_config")
    def test_reconcile_dry_run(self, mock_config, mock_conn, mock_subprocess):
        """reconcile --dry-run should show [DRY RUN] in output."""
        mock_config.return_value = NexusConfig()
        conn = _get_test_conn()
        mock_conn.return_value = conn
        # Broker unreachable
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = runner.invoke(app, ["reconcile", "--dry-run"])

        assert result.exit_code in (0, 1)
        assert "[DRY RUN]" in result.output


class TestInstallCommand:
    @patch("nexus.schedule.cron.CronTab")
    @patch("nexus.schedule.cron.shutil.which", return_value="/usr/local/bin/nexus")
    @patch("nexus.config.load_config")
    def test_install_command(self, mock_config, mock_which, mock_crontab_cls):
        """install command should report successful installation."""
        mock_config.return_value = NexusConfig()
        mock_cron = MagicMock()
        mock_job = MagicMock()
        mock_job.slices = "*/5 * * * *"
        mock_cron.new.return_value = mock_job
        mock_crontab_cls.return_value = mock_cron

        result = runner.invoke(app, ["install"])

        assert result.exit_code == 0
        assert "installed" in result.output.lower() or "Reconciler" in result.output


class TestUninstallCommand:
    @patch("nexus.schedule.cron.CronTab")
    def test_uninstall_command(self, mock_crontab_cls):
        """uninstall command should report removal."""
        mock_cron = MagicMock()
        mock_job = MagicMock()
        mock_cron.find_comment.return_value = iter([mock_job])
        mock_crontab_cls.return_value = mock_cron

        result = runner.invoke(app, ["uninstall"])

        assert result.exit_code == 0
        assert "removed" in result.output.lower() or "Removed" in result.output


class TestStatusCommand:
    @patch("nexus.schedule.cron.get_schedule_status")
    def test_status_command_installed(self, mock_status):
        """status command should show schedule info when installed."""
        mock_status.return_value = {
            "installed": True,
            "schedule": "*/5 * * * *",
            "command": "/usr/local/bin/nexus reconcile",
        }

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "installed" in result.output.lower()

    @patch("nexus.schedule.cron.get_schedule_status")
    def test_status_command_not_installed(self, mock_status):
        """status command should show not installed status."""
        mock_status.return_value = {"installed": False, "schedule": None, "command": None}

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()


class TestDoctorCommand:
    @patch("nexus.doctor.get_schedule_status", return_value={"installed": True, "schedule": "*/5 * * * *", "command": "nexus reconcile"})
    @patch("subprocess.run")
    @patch("nexus.db.get_connection")
    @patch("nexus.config.load_config")
    def test_doctor_command(self, mock_config, mock_conn, mock_subprocess, mock_cron_status):
        """doctor command should list check results."""
        mock_config.return_value = NexusConfig()
        conn = _get_test_conn()
        mock_conn.return_value = conn
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = runner.invoke(app, ["doctor"])

        # Should list checks (may pass or fail overall)
        assert result.exit_code in (0, 1)
        assert "db_valid" in result.output
