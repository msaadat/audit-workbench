from app import config


def test_dotenv_loads_values_without_overriding_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "# comments are ignored",
                "GROQ_API_KEY=file-key",
                "MISTRAL_API_KEY='quoted-key'",
                "LMSTUDIO_API_KEY=local-key # inline comment",
                "export WORKBENCH_DATA=C:\\Data\\Audit Workbench",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    monkeypatch.delenv("WORKBENCH_DATA", raising=False)

    config._load_env_file(dotenv)

    assert config.os.environ["GROQ_API_KEY"] == "env-key"
    assert config.os.environ["MISTRAL_API_KEY"] == "quoted-key"
    assert config.os.environ["LMSTUDIO_API_KEY"] == "local-key"
    assert config.os.environ["WORKBENCH_DATA"] == "C:\\Data\\Audit Workbench"
