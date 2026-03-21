# ATS platforms eligible for the optimized scan+map+fill pipeline.
# Lever validated across 8+ employers. Others commented out until validated.
ATS_PLATFORM_PATTERNS: list[tuple[str, str]] = [
    ("lever.co", "lever"),
    # ("myworkdayjobs.com", "workday"),
    # ("greenhouse.io", "greenhouse"),
    # ("icims.com", "icims"),
    # ("ashbyhq.com", "ashby"),
    # ("smartrecruiters.com", "smartrecruiters"),
    # ("workable.com", "workable"),
]


def detect_ats_platform(url_or_domain: str | None) -> str | None:
    """Detect ATS platform from a URL or domain string.

    Returns a short platform key (e.g., "lever") or None.
    """
    if not url_or_domain:
        return None
    lowered = url_or_domain.lower()
    for pattern, platform in ATS_PLATFORM_PATTERNS:
        if pattern in lowered:
            return platform
    return None
