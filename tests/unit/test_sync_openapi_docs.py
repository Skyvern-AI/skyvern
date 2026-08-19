from scripts import sync_openapi_docs
from skyvern.forge import api_app


class _FakeAPIApp:
    def openapi(self) -> dict[str, str]:
        print("LiteLLM diagnostic")
        return {"openapi": "3.1.0"}


def test_from_app_keeps_stdout_reserved_for_rendered_openapi(monkeypatch, capsys) -> None:
    monkeypatch.setattr(api_app, "create_api_app", _FakeAPIApp)

    assert sync_openapi_docs._from_app() == {"openapi": "3.1.0"}

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "LiteLLM diagnostic" in captured.err
