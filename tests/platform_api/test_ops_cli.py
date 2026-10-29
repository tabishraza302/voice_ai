from __future__ import annotations

import argparse
from datetime import timezone

import pytest

from platform_api.cli import _configured_limit, _parse_cutoff, _positive_batch_size, main


def test_ops_commands_are_listed_in_cli_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    for command in (
        "export-user",
        "delete-user-transcripts",
        "apply-retention",
        "backup",
        "restore",
    ):
        assert command in help_text


def test_cutoff_requires_timezone_and_normalizes_to_utc():
    cutoff = _parse_cutoff("2026-01-02T03:04:05-05:00")
    assert cutoff.tzinfo is timezone.utc
    assert cutoff.isoformat() == "2026-01-02T08:04:05+00:00"

    with pytest.raises(ValueError, match="include a timezone"):
        _parse_cutoff("2026-01-02T03:04:05")


def test_retention_batch_size_is_bounded():
    assert _positive_batch_size("1000") == 1000
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_batch_size("1001")


def test_cli_limits_cannot_exceed_deployment_configuration():
    assert _configured_limit(None, 500, "--batch-size") == 500
    assert _configured_limit(250, 500, "--batch-size") == 250

    with pytest.raises(argparse.ArgumentTypeError, match="configured maximum"):
        _configured_limit(501, 500, "--batch-size")
    with pytest.raises(argparse.ArgumentTypeError, match="must be positive"):
        _configured_limit(0, 500, "--batch-size")
