from fastapi import Depends, Request
from fastapi.routing import APIRoute


async def otp_bound_route_marker() -> None:
    """Declares a route one-time-code bound; the admission checks read it off the matched route."""


OTP_BOUND_ROUTE = Depends(otp_bound_route_marker)


def is_otp_bound_route(request: Request) -> bool:
    # Only the matched route's declaration counts, never the path, so a request with no matched route is unmarked.
    scope = getattr(request, "scope", None) or {}
    route = scope.get("route")
    return isinstance(route, APIRoute) and any(
        dependency.dependency is otp_bound_route_marker for dependency in route.dependencies
    )
