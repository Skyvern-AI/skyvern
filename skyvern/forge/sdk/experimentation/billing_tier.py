from __future__ import annotations

from enum import StrEnum

# Matched verbatim by experiment release conditions, so every routing surface has to spell the
# key and the values identically or a tier-targeted rollout silently misses one of them.
BILLING_TIER_PROPERTY = "billing_tier"


class BillingTier(StrEnum):
    ENTERPRISE = "enterprise"
    SELF_SERVE = "self_serve"
    # The tier could not be read. Distinct from ENTERPRISE so an outage is visible in telemetry
    # rather than reported as a real tier, and so a condition targeting self-serve cannot match it.
    UNKNOWN = "unknown"
