from types import SimpleNamespace

import pytest

from scripts.seed_dev_accounts import validate_target


def test_development_seed_rejects_nonlocal_or_non_development_databases():
    with pytest.raises(ValueError):
        validate_target(SimpleNamespace(env="production", database_url="postgresql+asyncpg://localhost/db"))
    with pytest.raises(ValueError):
        validate_target(SimpleNamespace(env="development", database_url="postgresql+asyncpg://remote.example/db"))
    validate_target(SimpleNamespace(env="development", database_url="postgresql+asyncpg://127.0.0.1/db"))


def test_demo_account_emails_are_accepted_by_authentication():
    import json
    from pathlib import Path
    from platform_api.auth.service import normalize_email_address

    accounts = json.loads(Path("web/src/lib/dev-accounts.json").read_text())
    for account in accounts:
        assert normalize_email_address(account["email"])[1] == account["email"]
