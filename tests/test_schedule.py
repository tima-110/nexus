"""Tests for nexus.schedule.cron — crontab management."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.schedule.cron import (
    CRON_COMMENT,
    get_schedule_status,
    install_schedule,
    uninstall_schedule,
)


class TestInstallSchedule:
    @patch("nexus.schedule.cron.CronTab")
    @patch("nexus.schedule.cron.shutil.which", return_value="/usr/local/bin/nexus")
    def test_install_creates_cron_entry(self, mock_which, mock_crontab_cls):
        """install_schedule should create a cron job with the correct command and schedule."""
        mock_cron = MagicMock()
        mock_job = MagicMock()
        mock_job.slices = "*/5 * * * *"
        mock_cron.new.return_value = mock_job
        mock_crontab_cls.return_value = mock_cron

        result = install_schedule(5)

        mock_which.assert_called_once_with("nexus")
        mock_cron.remove_all.assert_called_once_with(comment=CRON_COMMENT)
        mock_cron.new.assert_called_once_with(
            command="/usr/local/bin/nexus reconcile",
            comment=CRON_COMMENT,
        )
        mock_job.setall.assert_called_once_with("*/5 * * * *")
        mock_cron.write.assert_called_once()
        assert result == "*/5 * * * *"

    @patch("nexus.schedule.cron.CronTab")
    @patch("nexus.schedule.cron.shutil.which", return_value=None)
    def test_install_raises_if_no_nexus_binary(self, mock_which, mock_crontab_cls):
        """install_schedule should raise RuntimeError when nexus is not in PATH."""
        with pytest.raises(RuntimeError, match="Cannot find 'nexus'"):
            install_schedule()


class TestUninstallSchedule:
    @patch("nexus.schedule.cron.CronTab")
    def test_uninstall_removes_entry(self, mock_crontab_cls):
        """uninstall_schedule should remove the job and write when found."""
        mock_cron = MagicMock()
        mock_job = MagicMock()
        mock_cron.find_comment.return_value = iter([mock_job])
        mock_crontab_cls.return_value = mock_cron

        result = uninstall_schedule()

        assert result is True
        mock_cron.remove_all.assert_called_once_with(comment=CRON_COMMENT)
        mock_cron.write.assert_called_once()

    @patch("nexus.schedule.cron.CronTab")
    def test_uninstall_returns_false_if_not_found(self, mock_crontab_cls):
        """uninstall_schedule should return False when no matching job exists."""
        mock_cron = MagicMock()
        mock_cron.find_comment.return_value = iter([])
        mock_crontab_cls.return_value = mock_cron

        result = uninstall_schedule()

        assert result is False
        mock_cron.write.assert_not_called()


class TestGetScheduleStatus:
    @patch("nexus.schedule.cron.CronTab")
    def test_get_status_when_installed(self, mock_crontab_cls):
        """get_schedule_status should return installed=True with schedule details."""
        mock_cron = MagicMock()
        mock_job = MagicMock()
        mock_job.slices = "*/5 * * * *"
        mock_job.command = "/usr/local/bin/nexus reconcile"
        mock_cron.find_comment.return_value = iter([mock_job])
        mock_crontab_cls.return_value = mock_cron

        result = get_schedule_status()

        assert result["installed"] is True
        assert result["schedule"] == "*/5 * * * *"
        assert result["command"] == "/usr/local/bin/nexus reconcile"

    @patch("nexus.schedule.cron.CronTab")
    def test_get_status_when_not_installed(self, mock_crontab_cls):
        """get_schedule_status should return installed=False when no job found."""
        mock_cron = MagicMock()
        mock_cron.find_comment.return_value = iter([])
        mock_crontab_cls.return_value = mock_cron

        result = get_schedule_status()

        assert result["installed"] is False
        assert result["schedule"] is None
        assert result["command"] is None
