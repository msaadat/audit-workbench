from app import assistant_settings, llm


def test_agent_profile_defaults_to_assistant_settings(monkeypatch):
    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    settings = llm._settings("agent")
    assert settings.backend == "groq"
    assert settings.model == "llama-3.3-70b-versatile"


def test_agent_model_override(monkeypatch):
    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.setenv("AGENT_MODEL", "llama-3.1-8b-instant")
    settings = llm._settings("agent")
    assert settings.backend == "groq"
    assert settings.model == "llama-3.1-8b-instant"
    # The assistant profile is unaffected.
    assert llm._settings().model == "llama-3.3-70b-versatile"


def test_agent_provider_override_uses_provider_default_model(monkeypatch):
    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.setenv("AGENT_PROVIDER", "mistral")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    settings = llm._settings("agent")
    assert settings.backend == "mistral"
    assert settings.model == assistant_settings.PROVIDERS["mistral"]["default_model"]


def test_agent_status_reports_override(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "mistral")
    monkeypatch.setenv("AGENT_MODEL", "mistral-large-latest")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")
    status = llm.agent_status()
    assert status["configured"] is True
    assert status["provider"] == "mistral"
    assert status["model"] == "mistral-large-latest"


def test_agent_status_bad_provider_is_unconfigured(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "nonsense")
    status = llm.agent_status()
    assert status["configured"] is False
    assert "nonsense" in status["error"]
