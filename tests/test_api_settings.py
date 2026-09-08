import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.settings import load_settings


def test_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("EINTHUSAN_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="EINTHUSAN_API_KEY"):
        load_settings(core_config={})


def test_uses_injected_core_config(monkeypatch):
    monkeypatch.setenv("EINTHUSAN_API_KEY", "secret123")
    fake_cfg = {"radarr": {"url": "http://localhost:7878"}}

    settings = load_settings(core_config=fake_cfg)

    assert settings.api_key == "secret123"
    assert settings.core_config is fake_cfg


def test_loads_core_config_from_env_when_not_injected(monkeypatch, tmp_path):
    monkeypatch.setenv("EINTHUSAN_API_KEY", "secret123")
    import api.settings as settings_mod
    monkeypatch.setattr(settings_mod, "load_config", lambda: {"radarr": {"url": "http://real"}})

    settings = load_settings()

    assert settings.core_config == {"radarr": {"url": "http://real"}}
