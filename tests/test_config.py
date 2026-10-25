from pathlib import Path

import pytest

from editor.config import Config, ConfigError


def write_toml(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body)


def fake_keychain(values):
    def read(name: str) -> str | None:
        return values.get(name)
    return read


def test_loads_settings_and_keychain_secrets(tmp_path):
    home = tmp_path / "data"
    write_toml(home, 'portal_url = "https://portal.example.org/"\npackage_dir = "/pkg"\nagents = ["codex", "claude"]\n')
    cfg = Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({"editor_token": "tok"}))
    assert cfg.portal_url == "https://portal.example.org"
    assert cfg.package_dir == Path("/pkg")
    assert cfg.work_root == home / "jobs"
    assert cfg.house_rules_example.name == "house-rules.example.md"
    assert cfg.agents == ("codex", "claude") and cfg.max_budget_usd is None
    assert cfg.editor_token == "tok"
    assert cfg.codex_home == home / "codex-home" and cfg.grok_home == home / "grok-home"
    assert cfg.skill_dir.name == "infocus-show"


def test_only_portal_url_is_needed_and_the_token_is_optional(tmp_path):
    home = tmp_path / "data"
    write_toml(home, 'portal_url = "https://p.example.org"\n')
    cfg = Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({}))
    assert cfg.editor_token is None and cfg.package_dir == home / "package"
    write_toml(home, 'package_dir = "/pkg"\n')
    with pytest.raises(ConfigError) as err:
        Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({}))
    assert "portal_url" in str(err.value)


def test_default_agent_order_and_unknown_agent(tmp_path):
    home = tmp_path / "data"
    write_toml(home, 'portal_url = "https://p.example.org"\npackage_dir = "/pkg"\n')
    cfg = Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({"editor_token": "t"}))
    assert cfg.agents == ("codex", "claude", "grok")
    write_toml(home, 'portal_url = "https://p.example.org"\npackage_dir = "/pkg"\nagents = ["gpt"]\n')
    with pytest.raises(ConfigError):
        Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({"editor_token": "t"}))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError) as err:
        Config.load(env={"INFOCUS_EDITOR_HOME": str(tmp_path / "nope")}, keychain=fake_keychain({}))
    assert "config.toml" in str(err.value)


def test_repr_hides_secrets(tmp_path):
    home = tmp_path / "data"
    write_toml(home, 'portal_url = "https://p.example.org"\npackage_dir = "/pkg"\n')
    cfg = Config.load(env={"INFOCUS_EDITOR_HOME": str(home)},
                      keychain=fake_keychain({"editor_token": "SECRET1"}))
    assert "SECRET1" not in repr(cfg)


def test_machine_kind(tmp_path):
    home = tmp_path / "data"
    write_toml(home, 'portal_url = "https://p.example.org"\nmachine_kind = "personal"\n')
    assert Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({})).machine_kind == "personal"
    write_toml(home, 'portal_url = "https://p.example.org"\n')
    assert Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({})).machine_kind == "dedicated"
    write_toml(home, 'portal_url = "https://p.example.org"\nmachine_kind = "laptop"\n')
    with pytest.raises(ConfigError):
        Config.load(env={"INFOCUS_EDITOR_HOME": str(home)}, keychain=fake_keychain({}))


def test_an_old_portal_address_moves_to_the_current_one():
    from editor.config import portal_url
    assert portal_url("https://infocuspaly.com/") == "https://infocusportal.com"
    assert portal_url("https://portal.example.org") == "https://portal.example.org"
