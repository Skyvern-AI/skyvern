from skyvern.client.client import AsyncSkyvern, Skyvern


def test_clients_do_not_expose_removed_artifacts_subclient() -> None:
    """Artifact operations stay on the root clients, not a removed subclient."""
    assert callable(Skyvern.get_run_artifacts)
    assert callable(AsyncSkyvern.get_run_artifacts)
    assert not hasattr(Skyvern(api_key="test"), "artifacts")
    assert not hasattr(AsyncSkyvern(api_key="test"), "artifacts")
