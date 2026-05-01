from unittest.mock import patch

from skyvern.cli.quickstart import _COMPOSE_DATABASE_STRING, _bootstrap_compose_env_files


def test_bootstrap_compose_env_files_copies_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n")
    (tmp_path / "skyvern-frontend").mkdir()
    (tmp_path / "skyvern-frontend" / ".env.example").write_text("VITE_FOO=bar\n")

    _bootstrap_compose_env_files()

    assert (tmp_path / ".env").read_text() == "OPENAI_API_KEY=\n"
    assert (tmp_path / "skyvern-frontend" / ".env").read_text() == "VITE_FOO=bar\n"


def test_bootstrap_compose_env_files_does_not_overwrite_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n")
    (tmp_path / ".env").write_text("USER_VALUE=keep\n")

    _bootstrap_compose_env_files()

    assert (tmp_path / ".env").read_text() == "USER_VALUE=keep\n"


def test_bootstrap_rewrites_localhost_database_string_when_user_confirms(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'OPENAI_API_KEY=sk-dummy\nDATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern"\nOTHER=keep\n'
    )

    with patch("skyvern.cli.quickstart.Confirm.ask", return_value=True):
        _bootstrap_compose_env_files()

    new_content = (tmp_path / ".env").read_text()
    assert "localhost" not in new_content
    assert f'DATABASE_STRING="{_COMPOSE_DATABASE_STRING}"' in new_content
    assert "OPENAI_API_KEY=sk-dummy" in new_content
    assert "OTHER=keep" in new_content


def test_bootstrap_leaves_database_string_alone_when_user_declines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = 'OPENAI_API_KEY=sk-dummy\nDATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern"\n'
    (tmp_path / ".env").write_text(original)

    with patch("skyvern.cli.quickstart.Confirm.ask", return_value=False):
        _bootstrap_compose_env_files()

    assert (tmp_path / ".env").read_text() == original
