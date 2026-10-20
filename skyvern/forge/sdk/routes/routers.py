from fastapi import APIRouter

from skyvern.forge.sdk.services.route_authorization import ROUTE_AUTHORIZATION_DEPENDENCY

base_router = APIRouter(dependencies=[ROUTE_AUTHORIZATION_DEPENDENCY])
legacy_base_router = APIRouter(include_in_schema=False, dependencies=[ROUTE_AUTHORIZATION_DEPENDENCY])
legacy_v2_router = APIRouter(include_in_schema=False, dependencies=[ROUTE_AUTHORIZATION_DEPENDENCY])
