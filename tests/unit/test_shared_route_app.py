import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from tests.unit import shared_route_app


def test_lend_app_refuses_a_second_lend_and_catches_route_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Registries are keyed by id(app); fresh ones keep this throwaway app from poisoning a later app at its address.
    for registry in ("_lessees", "_route_baselines", "_build_settings", "_mutated_by"):
        monkeypatch.setattr(shared_route_app, registry, {})
    app = FastAPI()

    @app.get("/lent", tags=["lent"])
    async def lent() -> None:
        return None

    with pytest.raises(RuntimeError, match=r"mutated shared route internals of \['/lent'\]"):
        with pytest.MonkeyPatch.context() as borrower:
            shared_route_app.lend_app(app, borrower)
            with pytest.MonkeyPatch.context() as second_borrower:
                with pytest.raises(RuntimeError, match="already lent"):
                    shared_route_app.lend_app(app, second_borrower)
            lent_route = next(route for route in app.router.routes if isinstance(route, APIRoute))
            lent_route.tags.append("leaked")

    with pytest.MonkeyPatch.context() as next_borrower:
        with pytest.raises(RuntimeError, match="routes were mutated by"):
            shared_route_app.lend_app(app, next_borrower)
