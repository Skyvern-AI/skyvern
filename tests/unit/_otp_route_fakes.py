from types import SimpleNamespace

from fastapi.routing import APIRoute

from skyvern.forge.sdk.routes.otp_bound import OTP_BOUND_ROUTE


async def _endpoint() -> None:
    return None


def matched_route_request(path: str, *, otp_bound: bool) -> SimpleNamespace:
    route = APIRoute(path, _endpoint, methods=["POST"], dependencies=[OTP_BOUND_ROUTE] if otp_bound else None)
    return SimpleNamespace(scope={"route": route})
