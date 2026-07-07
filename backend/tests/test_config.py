from app import config


def test_dotenv_loads_values_without_overriding_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "# comments are ignored",
                "GROQ_API_KEY=file-key",
                "GROQ_MODEL='quoted-model'",
                "GROQ_BASE_URL=https://example.test/v1 # inline comment",
                "export WORKBENCH_DATA=C:\\Data\\Audit Workbench",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("GROQ_BASE_URL", raising=False)
    monkeypatch.delenv("WORKBENCH_DATA", raising=False)

    config._load_env_file(dotenv)

    assert config.os.environ["GROQ_API_KEY"] == "env-key"
    assert config.os.environ["GROQ_MODEL"] == "quoted-model"
    assert config.os.environ["GROQ_BASE_URL"] == "https://example.test/v1"
    assert config.os.environ["WORKBENCH_DATA"] == "C:\\Data\\Audit Workbench"
