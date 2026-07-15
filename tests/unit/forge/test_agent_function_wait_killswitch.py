from skyvern.forge.agent_functions import AgentFunction


def test_wait_time_optimization_disabled_by_default() -> None:
    assert AgentFunction().is_wait_time_optimization_enabled() is False
