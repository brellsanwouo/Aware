"""Environment helpers tests."""

from __future__ import annotations

from runtime.env import env_bool, resolve_max_agents


def test_ui_reloads_provider_and_model_after_dotenv_edit(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from runtime import env as env_module
    from ui.server import create_app

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWARE_LLM_PROVIDER=ollama\n"
        "OLLAMA_MODEL=gpt-oss:120b\n"
        "OPENAI_MODEL=gpt-5.6-luna\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWARE_ENV_FILE", str(env_file))
    monkeypatch.setattr(env_module, "_DOTENV_MANAGED", {})
    for key in ("AWARE_LLM_PROVIDER", "OLLAMA_MODEL", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)

    client = TestClient(create_app())
    first = client.get("/api/config").json()
    assert (first["llm_provider"], first["llm_model"]) == ("ollama-cloud", "gpt-oss:120b")

    env_file.write_text(
        "AWARE_LLM_PROVIDER=openai\n"
        "OPENAI_MODEL=gpt-5-mini\n",
        encoding="utf-8",
    )
    second = client.get("/api/config").json()
    assert (second["llm_provider"], second["llm_model"]) == ("openai", "gpt-5-mini")


def test_dotenv_refresh_preserves_shell_model_override(tmp_path, monkeypatch) -> None:
    from runtime import env as env_module

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setenv("AWARE_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPENAI_MODEL", "shell-model")
    monkeypatch.setattr(env_module, "_DOTENV_MANAGED", {})
    env_module.load_env()
    env_file.write_text("OPENAI_MODEL=another-file-model\n", encoding="utf-8")
    env_module.refresh_env()
    assert env_module.os.environ["OPENAI_MODEL"] == "shell-model"


def test_env_bool_parsing(monkeypatch) -> None:
    monkeypatch.setenv("AWARE_ENABLE_REASONING", "true")
    assert env_bool("AWARE_ENABLE_REASONING", False) is True

    monkeypatch.setenv("AWARE_ENABLE_REASONING", "0")
    assert env_bool("AWARE_ENABLE_REASONING", True) is False

    monkeypatch.setenv("AWARE_ENABLE_REASONING", "invalid")
    assert env_bool("AWARE_ENABLE_REASONING", True) is True


def test_max_agents_uses_configured_total_and_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTOR_MAX_AGENTS", "3")
    assert resolve_max_agents() == 3
    assert resolve_max_agents(2) == 2
    monkeypatch.setenv("EXECUTOR_MAX_AGENTS", "invalid")
    assert resolve_max_agents() == 5


def test_ui_reloads_agent_limit_after_dotenv_edit(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from runtime import env as env_module
    from ui.server import create_app

    env_file = tmp_path / ".env"
    env_file.write_text("EXECUTOR_MAX_AGENTS=3\n", encoding="utf-8")
    monkeypatch.setenv("AWARE_ENV_FILE", str(env_file))
    monkeypatch.setattr(env_module, "_DOTENV_MANAGED", {})
    monkeypatch.delenv("EXECUTOR_MAX_AGENTS", raising=False)

    client = TestClient(create_app())
    assert client.get("/api/config").json()["executor_max_agents"] == "3"
    env_file.write_text("EXECUTOR_MAX_AGENTS=4\n", encoding="utf-8")
    assert client.get("/api/config").json()["executor_max_agents"] == "4"
