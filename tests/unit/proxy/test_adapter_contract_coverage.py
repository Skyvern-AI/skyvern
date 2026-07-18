"""Every shipped adapter must be bound to its port's contract suite.

A contract is only "required for adapter changes" if shipping an adapter WITHOUT one
fails. Participation was a hand-maintained list of subclasses, so a new adapter with no
binding sailed through green — not because it satisfied the contract, but because the
contract never ran against it. Nothing announced the gap, because the gap was silence.

So the shipped implementations are discovered by walking the package rather than listed
here: a new adapter is covered the moment it exists, and stays red until someone binds
it to its port's contract.

Cloud adapters get the same treatment in tests/cloud/ — this file cannot import them
(tests/unit is OSS-synced, and skyvern/ may not know cloud/ exists).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import skyvern.proxy.adapters
from skyvern.proxy.adapters.memory import AllowAllAuth
from skyvern.proxy.ports import AuthPort, EventPolicyPort, MetricsPort, SessionRegistryPort, UpstreamBrowserPort
from tests.unit.proxy.test_auth_contract import AuthPortContract
from tests.unit.proxy.test_event_policy_contract import EventPolicyPortContract
from tests.unit.proxy.test_metrics_contract import MetricsPortContract
from tests.unit.proxy.test_session_registry_contract import SessionRegistryContract
from tests.unit.proxy.test_upstream_browser_port_contract import UpstreamBrowserPortContract

# Each driven port, the reusable contract every adapter of it must pass, and the factory
# that contract calls to get one.
PORT_CONTRACTS = (
    (UpstreamBrowserPort, UpstreamBrowserPortContract, "make_port"),
    (SessionRegistryPort, SessionRegistryContract, "make_registry"),
    (AuthPort, AuthPortContract, "make_auth"),
    (MetricsPort, MetricsPortContract, "make_metrics"),
    (EventPolicyPort, EventPolicyPortContract, "make_policy"),
)


# Adapters that deliberately do NOT satisfy their port's contract, each with the reason.
# An entry here is a decision a reviewer can see and argue with; what this file replaces
# was silence. It is not a way to make a red go away — every exemption owes a test below
# proving its stated reason is still true, so an adapter cannot quietly drift in here.
CONTRACT_EXEMPT: dict[type, str] = {
    AllowAllAuth: (
        "A deliberate authentication bypass for the local-dev entrypoint "
        "(skyvern/proxy/__main__.py): it returns a principal for every caller, including one "
        "presenting no credential at all, so it cannot satisfy AuthPortContract's authenticate "
        "clauses and must never be reachable from the cloud wiring."
    ),
}


def _adapter_classes() -> list[type]:
    """Every class the adapters package defines itself (not what it imports)."""
    found: list[type] = []
    for module_info in pkgutil.iter_modules(skyvern.proxy.adapters.__path__, "skyvern.proxy.adapters."):
        module = importlib.import_module(module_info.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module_info.name and not cls.__name__.startswith("_"):
                found.append(cls)
    return found


def _implements(cls: type, port: type) -> bool:
    # The ports are runtime_checkable method-only Protocols, so a structural check is
    # exactly the question being asked: does this class present the port's surface?
    try:
        return issubclass(cls, port)
    except TypeError:
        return False


def _all_subclasses(base: type) -> set[type]:
    direct = set(base.__subclasses__())
    return direct.union(*(_all_subclasses(child) for child in direct)) if direct else set()


def _bound_adapters(contract: type, factory: str) -> set[type]:
    """The adapter types actually exercised by a collected contract suite.

    Derived from the suites themselves rather than a list, so an adapter can only be
    "covered" by really being run through its contract.
    """
    bound: set[type] = set()
    for subclass in _all_subclasses(contract):
        # Only pytest-collected suites count; the deliberately broken fixtures in
        # test_contract_teeth.py subclass these contracts too and prove nothing here.
        if not subclass.__name__.startswith("Test"):
            continue
        bound.add(type(getattr(subclass(), factory)()))
    return bound


@pytest.mark.parametrize(
    ("port", "contract", "factory"), PORT_CONTRACTS, ids=[entry[0].__name__ for entry in PORT_CONTRACTS]
)
def test_every_shipped_adapter_is_bound_to_its_port_contract(port: type, contract: type, factory: str) -> None:
    shipped = {cls for cls in _adapter_classes() if _implements(cls, port)}
    unbound = shipped - _bound_adapters(contract, factory) - set(CONTRACT_EXEMPT)
    assert not unbound, (
        f"{sorted(cls.__name__ for cls in unbound)} implement {port.__name__} but no collected "
        f"{contract.__name__} subclass runs them. Bind each to its contract — an adapter no "
        f"contract runs against is not covered, it is merely unexamined. If it genuinely cannot "
        f"conform, add it to CONTRACT_EXEMPT with the reason and a test pinning that reason."
    )


@pytest.mark.asyncio
async def test_the_allow_all_auth_exemption_still_describes_what_it_does() -> None:
    """An exemption is only honest while its stated reason is true.

    AllowAllAuth is exempt because it authenticates everyone by design. The day it starts
    checking credentials, that reason is false and it belongs under the contract like any
    other adapter — so this fails and says so, rather than leaving a stale excuse behind.
    """
    assert await AllowAllAuth().authenticate({}) is not None, (
        "AllowAllAuth no longer authenticates a caller presenting no credential, so its "
        "CONTRACT_EXEMPT reason is stale: bind it to AuthPortContract instead."
    )


def test_the_coverage_check_can_actually_find_an_adapter() -> None:
    """Guard on the guard: if the package walk returned nothing, every assertion above
    would pass vacuously and the whole file would be decoration."""
    assert len(_adapter_classes()) > 5
    assert any(_implements(cls, UpstreamBrowserPort) for cls in _adapter_classes())


def test_every_exemption_names_an_adapter_that_still_exists() -> None:
    """A stale exemption silently widens the escape hatch for a class nobody ships."""
    shipped = set(_adapter_classes())
    assert set(CONTRACT_EXEMPT) <= shipped, (
        f"exemptions for adapters that no longer exist: {sorted(cls.__name__ for cls in set(CONTRACT_EXEMPT) - shipped)}"
    )
