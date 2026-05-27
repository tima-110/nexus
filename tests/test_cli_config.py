"""Tests for config commands (show, set, path)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app

runner = CliRunner()


class TestConfigPath:
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_path_shows_path(self, mock_path, tmp_path):
        """config path shows the config file path."""
        config_file = tmp_path / "config.toml"
        mock_path.return_value = config_file

        result = runner.invoke(app, ["config", "path"])

        assert result.exit_code == 0
        assert str(config_file) in result.output


class TestConfigShow:
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_show_displays_toml(self, mock_path, tmp_path):
        """config show displays TOML content when file exists."""
        config_file = tmp_path / "config.toml"
        toml_content = "[reconciler]\ninterval_minutes = 5\nmarket_hours_only = false\n\n[order]\nslippage_buffer_percent = 1.5\nmarket_order_wait_seconds = 5\n\n[database]\npath = \"~/.local/share/nexus/nexus.db\"\n\n[audit_log]\npath = \"~/.local/share/nexus/audit.jsonl\"\n"
        config_file.write_text(toml_content, encoding="utf-8")
        mock_path.return_value = config_file

        result = runner.invoke(app, ["config", "show"])

        assert result.exit_code == 0
        assert "reconciler" in result.output
        assert "interval_minutes" in result.output

    @patch("nexus.config._config_file_path")
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_show_json(self, mock_cmd_path, mock_config_path, tmp_path):
        """--json config show returns JSON with reconciler key."""
        config_file = tmp_path / "config.toml"
        toml_content = "[reconciler]\ninterval_minutes = 5\nmarket_hours_only = false\n\n[order]\nslippage_buffer_percent = 1.5\nmarket_order_wait_seconds = 5\n\n[database]\npath = \"~/.local/share/nexus/nexus.db\"\n\n[audit_log]\npath = \"~/.local/share/nexus/audit.jsonl\"\n"
        config_file.write_text(toml_content, encoding="utf-8")
        mock_cmd_path.return_value = config_file
        mock_config_path.return_value = config_file

        result = runner.invoke(app, ["--json", "config", "show"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "reconciler" in data
        assert "interval_minutes" in data["reconciler"]
        assert data["reconciler"]["interval_minutes"] == 5


class TestConfigSet:
    @patch("nexus.config._config_file_path")
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_set_updates_value(self, mock_cmd_path, mock_config_path, tmp_path):
        """config set updates a value in the config file."""
        config_file = tmp_path / "config.toml"
        toml_content = "[reconciler]\ninterval_minutes = 5\nmarket_hours_only = false\n\n[order]\nslippage_buffer_percent = 1.5\nmarket_order_wait_seconds = 5\n\n[database]\npath = \"~/.local/share/nexus/nexus.db\"\n\n[audit_log]\npath = \"~/.local/share/nexus/audit.jsonl\"\n"
        config_file.write_text(toml_content, encoding="utf-8")
        mock_cmd_path.return_value = config_file
        mock_config_path.return_value = config_file

        result = runner.invoke(app, ["config", "set", "reconciler.interval_minutes", "10"])

        assert result.exit_code == 0
        assert "Set" in result.output or "10" in result.output

        # Verify the file was updated
        updated = config_file.read_text(encoding="utf-8")
        assert "10" in updated

    @patch("nexus.config._config_file_path")
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_set_invalid_key_errors(self, mock_cmd_path, mock_config_path, tmp_path):
        """config set with invalid key shows error."""
        config_file = tmp_path / "config.toml"
        toml_content = "[reconciler]\ninterval_minutes = 5\nmarket_hours_only = false\n\n[order]\nslippage_buffer_percent = 1.5\nmarket_order_wait_seconds = 5\n\n[database]\npath = \"~/.local/share/nexus/nexus.db\"\n\n[audit_log]\npath = \"~/.local/share/nexus/audit.jsonl\"\n"
        config_file.write_text(toml_content, encoding="utf-8")
        mock_cmd_path.return_value = config_file
        mock_config_path.return_value = config_file

        result = runner.invoke(app, ["config", "set", "reconciler.nonexistent", "val"])

        assert result.exit_code == 1

    @patch("nexus.config._config_file_path")
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_set_invalid_section_errors(self, mock_cmd_path, mock_config_path, tmp_path):
        """config set with invalid section shows error."""
        config_file = tmp_path / "config.toml"
        toml_content = "[reconciler]\ninterval_minutes = 5\nmarket_hours_only = false\n\n[order]\nslippage_buffer_percent = 1.5\nmarket_order_wait_seconds = 5\n\n[database]\npath = \"~/.local/share/nexus/nexus.db\"\n\n[audit_log]\npath = \"~/.local/share/nexus/audit.jsonl\"\n"
        config_file.write_text(toml_content, encoding="utf-8")
        mock_cmd_path.return_value = config_file
        mock_config_path.return_value = config_file

        result = runner.invoke(app, ["config", "set", "bogus.field", "val"])

        assert result.exit_code == 1

    @patch("nexus.config._config_file_path")
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_config_set_bad_format_key_errors(self, mock_cmd_path, mock_config_path, tmp_path):
        """config set with key not in section.field format shows error."""
        config_file = tmp_path / "config.toml"
        toml_content = "[reconciler]\ninterval_minutes = 5\n"
        config_file.write_text(toml_content, encoding="utf-8")
        mock_cmd_path.return_value = config_file
        mock_config_path.return_value = config_file

        result = runner.invoke(app, ["config", "set", "justoneword", "val"])

        assert result.exit_code == 1
