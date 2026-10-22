import base64

from app import assistant_settings, llm


def test_agent_profile_defaults_to_assistant_settings(monkeypatch):
    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    settings = llm._settings("agent")
    assert settings.backend == "groq"
    assert settings.model == "llama-3.3-70b-versatile"


def test_vision_profile_and_image_part(monkeypatch):
    monkeypatch.delenv("AGENT_VISION_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_VISION_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_VISION_MODEL", raising=False)
    monkeypatch.delenv("AGENT_VISION_CAPABILITIES", raising=False)
    assistant_settings.save({
        "provider": "mistral",
        "model": "mistral-large-latest",
        "vision_profile": None,
    })
    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    settings = llm._settings("vision")
    assert settings.model == "pixtral-large-latest"
    status = llm.agent_status()
    assert status["vision_configured"] is True
    part = llm.image_part(b"image", "image/png")
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == b"image"


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


def test_agent_and_vision_profiles_call_the_overridden_endpoint(monkeypatch):
    """Runs reach the provider through their snapshot profile, not the live
    settings, so the override must hold on that path as well."""

    monkeypatch.setenv("AGENT_PROVIDER", "lmstudio")
    monkeypatch.setenv("AGENT_MODEL", "local-text")
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "lmstudio")
    monkeypatch.setenv("AGENT_VISION_MODEL", "local-vision")
    monkeypatch.setenv("AGENT_VISION_CAPABILITIES", "vision")
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    default_hashes = {
        name: llm.resolve_model_profile(name).profile_hash for name in ("agent", "vision")
    }

    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://gpu01.internal:1234/v1")
    endpoint = "http://gpu01.internal:1234/v1"
    snapshot = llm.model_profile_snapshot()

    assert snapshot["text"]["base_url"] == endpoint
    assert snapshot["vision"]["base_url"] == endpoint
    assert llm._settings("agent").base_url == endpoint
    assert llm._settings("vision").base_url == endpoint
    assert llm._settings(snapshot["text"]).base_url == endpoint
    status = llm.agent_status()
    assert status["base_url"] == endpoint
    assert status["vision_configured"] is True
    # Moving the server moves the endpoint, not the model: the profile
    # identity durable proposals are keyed on must not change with it.
    assert snapshot["text"]["profile_hash"] == default_hashes["agent"]
    assert snapshot["vision"]["profile_hash"] == default_hashes["vision"]


def test_an_invalid_endpoint_leaves_the_agent_unconfigured(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "lmstudio")
    monkeypatch.setenv("AGENT_MODEL", "local-text")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "ftp://gpu01.internal/v1")
    status = llm.agent_status()
    assert status["configured"] is False
    assert "LMSTUDIO_BASE_URL" in status["error"]
