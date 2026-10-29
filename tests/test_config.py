import pytest

from tradebot.config import load_settings


def test_defaults_are_safe(monkeypatch):
    for k in ["KALSHI_ENV", "DRY_RUN", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"]:
        monkeypatch.delenv(k, raising=False)
    s = load_settings(env_file=None)
    assert s.kalshi_env == "demo"
    assert s.dry_run is True
    assert s.max_bet_default == 20 and s.manual_approval_above == 500
    assert not s.has_credentials


def test_rejects_unknown_env(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "live")
    with pytest.raises(ValueError):
        load_settings(env_file=None)
