from __future__ import annotations

from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from skyvern.config import settings


class ProxyLocation(StrEnum):
    RESIDENTIAL = "RESIDENTIAL"
    US_CA = "US-CA"
    US_NY = "US-NY"
    US_TX = "US-TX"
    US_FL = "US-FL"
    US_WA = "US-WA"
    RESIDENTIAL_ES = "RESIDENTIAL_ES"
    RESIDENTIAL_IE = "RESIDENTIAL_IE"
    RESIDENTIAL_GB = "RESIDENTIAL_GB"
    RESIDENTIAL_IN = "RESIDENTIAL_IN"
    RESIDENTIAL_JP = "RESIDENTIAL_JP"
    RESIDENTIAL_FR = "RESIDENTIAL_FR"
    RESIDENTIAL_DE = "RESIDENTIAL_DE"
    RESIDENTIAL_NZ = "RESIDENTIAL_NZ"
    RESIDENTIAL_ZA = "RESIDENTIAL_ZA"
    RESIDENTIAL_AR = "RESIDENTIAL_AR"
    RESIDENTIAL_AU = "RESIDENTIAL_AU"
    RESIDENTIAL_BR = "RESIDENTIAL_BR"
    RESIDENTIAL_TR = "RESIDENTIAL_TR"
    RESIDENTIAL_CA = "RESIDENTIAL_CA"
    RESIDENTIAL_MX = "RESIDENTIAL_MX"
    RESIDENTIAL_IT = "RESIDENTIAL_IT"
    RESIDENTIAL_NL = "RESIDENTIAL_NL"
    RESIDENTIAL_PH = "RESIDENTIAL_PH"
    RESIDENTIAL_KR = "RESIDENTIAL_KR"
    RESIDENTIAL_SA = "RESIDENTIAL_SA"
    RESIDENTIAL_ISP = "RESIDENTIAL_ISP"
    NONE = "NONE"

    @staticmethod
    def get_zone(proxy_location: ProxyLocation) -> str:
        zone_mapping = {
            ProxyLocation.US_CA: "california",
            ProxyLocation.US_NY: "newyork",
            ProxyLocation.US_TX: "texas",
            ProxyLocation.US_FL: "florida",
            ProxyLocation.US_WA: "washington",
            ProxyLocation.RESIDENTIAL: "residential_long-country-us",
        }
        if proxy_location in zone_mapping:
            return zone_mapping[proxy_location]
        raise ValueError(f"No zone mapping for proxy location: {proxy_location}")

    @classmethod
    def residential_country_locations(cls) -> set[ProxyLocation]:
        return {
            cls.RESIDENTIAL,
            cls.RESIDENTIAL_ES,
            cls.RESIDENTIAL_IE,
            cls.RESIDENTIAL_GB,
            cls.RESIDENTIAL_IN,
            cls.RESIDENTIAL_JP,
            cls.RESIDENTIAL_FR,
            cls.RESIDENTIAL_DE,
            cls.RESIDENTIAL_NZ,
            cls.RESIDENTIAL_ZA,
            cls.RESIDENTIAL_AR,
            cls.RESIDENTIAL_AU,
            cls.RESIDENTIAL_BR,
            cls.RESIDENTIAL_TR,
            cls.RESIDENTIAL_CA,
            cls.RESIDENTIAL_MX,
            cls.RESIDENTIAL_IT,
            cls.RESIDENTIAL_NL,
            cls.RESIDENTIAL_PH,
            cls.RESIDENTIAL_KR,
            cls.RESIDENTIAL_SA,
        }

    @staticmethod
    def get_proxy_count(proxy_location: ProxyLocation) -> int:
        counts = {
            ProxyLocation.RESIDENTIAL: 10000,
            ProxyLocation.RESIDENTIAL_ES: 2000,
            ProxyLocation.RESIDENTIAL_IE: 2000,
            ProxyLocation.RESIDENTIAL_GB: 2000,
            ProxyLocation.RESIDENTIAL_IN: 2000,
            ProxyLocation.RESIDENTIAL_JP: 2000,
            ProxyLocation.RESIDENTIAL_FR: 2000,
            ProxyLocation.RESIDENTIAL_DE: 2000,
            ProxyLocation.RESIDENTIAL_NZ: 2000,
            ProxyLocation.RESIDENTIAL_ZA: 2000,
            ProxyLocation.RESIDENTIAL_AR: 2000,
            ProxyLocation.RESIDENTIAL_AU: 2000,
            ProxyLocation.RESIDENTIAL_BR: 2000,
            ProxyLocation.RESIDENTIAL_TR: 2000,
            ProxyLocation.RESIDENTIAL_CA: 2000,
            ProxyLocation.RESIDENTIAL_MX: 2000,
            ProxyLocation.RESIDENTIAL_IT: 2000,
            ProxyLocation.RESIDENTIAL_NL: 2000,
            ProxyLocation.RESIDENTIAL_PH: 2000,
            ProxyLocation.RESIDENTIAL_KR: 2000,
            ProxyLocation.RESIDENTIAL_SA: 2000,
        }
        return counts.get(proxy_location, 10000)

    @staticmethod
    def get_country_code(proxy_location: ProxyLocation) -> str:
        mapping = {
            ProxyLocation.RESIDENTIAL: "US",
            ProxyLocation.RESIDENTIAL_ES: "ES",
            ProxyLocation.RESIDENTIAL_IE: "IE",
            ProxyLocation.RESIDENTIAL_GB: "GB",
            ProxyLocation.RESIDENTIAL_IN: "IN",
            ProxyLocation.RESIDENTIAL_JP: "JP",
            ProxyLocation.RESIDENTIAL_FR: "FR",
            ProxyLocation.RESIDENTIAL_DE: "DE",
            ProxyLocation.RESIDENTIAL_NZ: "NZ",
            ProxyLocation.RESIDENTIAL_ZA: "ZA",
            ProxyLocation.RESIDENTIAL_AR: "AR",
            ProxyLocation.RESIDENTIAL_AU: "AU",
            ProxyLocation.RESIDENTIAL_BR: "BR",
            ProxyLocation.RESIDENTIAL_TR: "TR",
            ProxyLocation.RESIDENTIAL_CA: "CA",
            ProxyLocation.RESIDENTIAL_MX: "MX",
            ProxyLocation.RESIDENTIAL_IT: "IT",
            ProxyLocation.RESIDENTIAL_NL: "NL",
            ProxyLocation.RESIDENTIAL_PH: "PH",
            ProxyLocation.RESIDENTIAL_KR: "KR",
            ProxyLocation.RESIDENTIAL_SA: "SA",
        }
        return mapping.get(proxy_location, "US")


# Supported countries for granular geo-targeting.
SUPPORTED_GEO_COUNTRIES = frozenset(
    {
        "US",
        "AR",
        "AU",
        "BR",
        "CA",
        "DE",
        "ES",
        "FR",
        "GB",
        "IE",
        "IN",
        "IT",
        "JP",
        "MX",
        "NL",
        "NZ",
        "PH",
        "KR",
        "SA",
        "TR",
        "ZA",
    }
)


class GeoTarget(BaseModel):
    """Granular proxy geo-targeting request with country, optional subdivision, and optional city."""

    country: str = Field(
        description="ISO 3166-1 alpha-2 country code (e.g., 'US', 'GB', 'DE')",
        examples=["US", "GB", "DE", "FR"],
        min_length=2,
        max_length=2,
    )
    subdivision: str | None = Field(
        default=None,
        description="ISO 3166-2 subdivision code without country prefix (e.g., 'CA' for California, 'NY' for New York)",
        examples=["CA", "NY", "TX", "ENG"],
        max_length=10,
    )
    city: str | None = Field(
        default=None,
        description="City name in English from GeoNames (e.g., 'New York', 'Los Angeles', 'London')",
        examples=["New York", "Los Angeles", "London", "Berlin"],
        max_length=100,
    )

    @field_validator("country")
    @classmethod
    def validate_country(cls, v: str) -> str:
        """Validate country is in supported list and normalize to uppercase."""
        v = v.upper()
        if v not in SUPPORTED_GEO_COUNTRIES:
            raise ValueError(
                f"Country '{v}' is not supported for geo targeting. "
                f"Supported countries: {sorted(SUPPORTED_GEO_COUNTRIES)}"
            )
        return v

    @field_validator("subdivision")
    @classmethod
    def validate_subdivision(cls, v: str | None) -> str | None:
        """Normalize subdivision code to uppercase and strip country prefix if present."""
        if v is None:
            return v
        v = v.upper()
        # Strip country prefix if accidentally included (e.g., "US-CA" -> "CA")
        if "-" in v:
            v = v.split("-", 1)[1]
        return v


ResolvedProxyLocationInput = ProxyLocation | GeoTarget | dict[str, Any]
ProxyLocationInput = ResolvedProxyLocationInput | None


def runtime_proxy_location(proxy_location: ProxyLocationInput) -> ResolvedProxyLocationInput:
    if proxy_location is None:
        if not settings.RUNTIME_PROXY_DEFAULT_NONE_ENABLED:
            return ProxyLocation.RESIDENTIAL
        return ProxyLocation.NONE
    return proxy_location


def proxy_location_to_request(proxy_location: ProxyLocationInput) -> ProxyLocation | dict[str, Any] | None:
    if isinstance(proxy_location, GeoTarget):
        return proxy_location.model_dump()
    return proxy_location


def get_tzinfo_from_proxy(proxy_location: ProxyLocation) -> ZoneInfo | None:
    if proxy_location == ProxyLocation.NONE:
        return None

    if proxy_location == ProxyLocation.US_CA:
        return ZoneInfo("America/Los_Angeles")

    if proxy_location == ProxyLocation.US_NY:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_TX:
        return ZoneInfo("America/Chicago")

    if proxy_location == ProxyLocation.US_FL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_WA:
        return ZoneInfo("America/Los_Angeles")

    if proxy_location == ProxyLocation.RESIDENTIAL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL_ES:
        return ZoneInfo("Europe/Madrid")

    if proxy_location == ProxyLocation.RESIDENTIAL_IE:
        return ZoneInfo("Europe/Dublin")

    if proxy_location == ProxyLocation.RESIDENTIAL_GB:
        return ZoneInfo("Europe/London")

    if proxy_location == ProxyLocation.RESIDENTIAL_IN:
        return ZoneInfo("Asia/Kolkata")

    if proxy_location == ProxyLocation.RESIDENTIAL_JP:
        return ZoneInfo("Asia/Tokyo")

    if proxy_location == ProxyLocation.RESIDENTIAL_FR:
        return ZoneInfo("Europe/Paris")

    if proxy_location == ProxyLocation.RESIDENTIAL_DE:
        return ZoneInfo("Europe/Berlin")

    if proxy_location == ProxyLocation.RESIDENTIAL_NZ:
        return ZoneInfo("Pacific/Auckland")

    if proxy_location == ProxyLocation.RESIDENTIAL_ZA:
        return ZoneInfo("Africa/Johannesburg")

    if proxy_location == ProxyLocation.RESIDENTIAL_AR:
        return ZoneInfo("America/Argentina/Buenos_Aires")

    if proxy_location == ProxyLocation.RESIDENTIAL_AU:
        return ZoneInfo("Australia/Sydney")

    if proxy_location == ProxyLocation.RESIDENTIAL_BR:
        return ZoneInfo("America/Sao_Paulo")

    if proxy_location == ProxyLocation.RESIDENTIAL_TR:
        return ZoneInfo("Europe/Istanbul")

    if proxy_location == ProxyLocation.RESIDENTIAL_CA:
        return ZoneInfo("America/Toronto")

    if proxy_location == ProxyLocation.RESIDENTIAL_MX:
        return ZoneInfo("America/Mexico_City")

    if proxy_location == ProxyLocation.RESIDENTIAL_IT:
        return ZoneInfo("Europe/Rome")

    if proxy_location == ProxyLocation.RESIDENTIAL_NL:
        return ZoneInfo("Europe/Amsterdam")

    if proxy_location == ProxyLocation.RESIDENTIAL_PH:
        return ZoneInfo("Asia/Manila")

    if proxy_location == ProxyLocation.RESIDENTIAL_KR:
        return ZoneInfo("Asia/Seoul")

    if proxy_location == ProxyLocation.RESIDENTIAL_SA:
        return ZoneInfo("Asia/Riyadh")

    if proxy_location == ProxyLocation.RESIDENTIAL_ISP:
        return ZoneInfo("America/New_York")

    return None
