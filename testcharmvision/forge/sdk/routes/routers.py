from fastapi import APIRouter

base_router = APIRouter()
legacy_base_router = APIRouter(include_in_schema=False)
legacy_v2_router = APIRouter(include_in_schema=False)
