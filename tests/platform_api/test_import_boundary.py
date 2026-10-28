"""Tests for import boundaries and module isolation in platform_api.

Ensures that importing platform_api or any of its control-plane modules
never loads GPU model weights, local MLX runtime, or legacy bot.db.
"""

import subprocess
import sys


def test_platform_import_does_not_load_models():
    """Verify importing platform_api.app does not load heavy ML or bot.db."""
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import platform_api.app; import sys; "
            "assert 'mlx.core' not in sys.modules, f'mlx.core imported: {sys.modules.get(\"mlx.core\")}'; "
            "assert 'bot.db.session' not in sys.modules, f'bot.db.session imported: {sys.modules.get(\"bot.db.session\")}'",
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_platform_db_import_does_not_load_bot_db():
    """Verify platform_api.db has no dependency on bot.db."""
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import platform_api.db.base; import platform_api.db.session; import sys; "
            "assert 'bot.db' not in sys.modules, 'bot.db must not be imported by platform_api.db'",
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_platform_catalog_does_not_import_gpu_services():
    """Verify model catalog loading does not import GPU services."""
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import platform_api.catalog; import sys; "
            "assert 'mlx' not in sys.modules, 'mlx must not be imported by platform_api.catalog'; "
            "assert 'bot.services' not in sys.modules, 'bot.services must not be imported by platform_api.catalog'",
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
