from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app


def test_dummy_agent() -> None:
    start_forge_app()
    print(app.agent)
    return
