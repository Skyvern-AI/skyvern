"""The first event-filtering policy pack (SKY-12501), as data for the engine.

Two kinds of rule, and the difference matters:

- An UNCONDITIONAL drop, for an event no CDP client reads at all. Safe for everyone
  precisely because it does not depend on knowing anything about the client.
- An interest-relaxable THROTTLE, for an event clients do read but which a page can
  emit without bound. Observed `<Domain>.enable` lifts the cap; never seeing one only
  leaves the cap in place. Interest may only RELAX a rule (see `is_domain_enabled`) —
  a rule that tightened on False would eventually eat traffic a client asked for,
  because "not observed" and "not wanted" are the same value.

The reduction comes from the drop set. A throttle is a ceiling on pathological volume,
not a lever: every client library enables the domains it uses, so the relax path is the
normal path and the cap is what remains for a stream nobody claimed.

Enabling this pack is a deployment choice (`CDP_PROXY_EVENT_POLICY=noisy-v1`); the
proxy still defaults to forwarding everything unchanged.
"""

from __future__ import annotations

from skyvern.proxy.core.policy import EventPolicyConfig, RateRule

# Events that neither the pinned Playwright driver nor puppeteer-core references
# ANYWHERE — verified by enumerating every `Network.*` symbol in both sources rather
# than by sampling the ones we expected to find:
#
#   Network.dataReceived            per-chunk byte progress (requestId, dataLength,
#                                   encodedDataLength, timestamp). A body's real size
#                                   arrives on responseReceived/loadingFinished, which
#                                   is what clients actually read; this only reports
#                                   how the transfer was split up on the way.
#   Network.resourceChangedPriority the scheduler re-ranked a pending request.
#                                   Diagnostic; no client models request priority.
#
# Both fire only for a session that enabled Network — i.e. they are noise WITHIN a
# stream the client wanted, which is why interest cannot be what gates them.
#
# Adding to this list means proving the same thing about the new method: grep the
# pinned client sources, and keep tests/unit/proxy/test_policy_pack.py's consumed-set
# guard green. Everything a client reads is off limits no matter how noisy it looks —
# Network.loadingFinished and the *ExtraInfo pair are high-frequency AND load-bearing.
UNCONSUMED_EVENTS = (
    "Network.dataReceived",
    "Network.resourceChangedPriority",
)

# A page in a console loop can emit console events as fast as it can run, and each one
# carries its serialized arguments. Console output is diagnostic and already lossy, so
# a ceiling is safe where one on a structural stream would not be. Set well above what
# a page emits normally: this is a runaway guard, not a budget anyone should feel.
CONSOLE_BURST_PER_SECOND = 100

NOISY_EVENT_PACK_V1 = EventPolicyConfig(
    version=1,
    rules=(
        *(RateRule.drop(method) for method in UNCONSUMED_EVENTS),
        RateRule.throttle(
            "Runtime.consoleAPICalled",
            max_per_window=CONSOLE_BURST_PER_SECOND,
            window_seconds=1.0,
            relax_when_enabled=True,
        ),
    ),
)

# The event methods this pack gates, for the driving adapter's metric-label allowlist.
# Derived from the rules so the two cannot drift: a rule whose method is missing from
# the allowlist reports its drops as cdp_method="other" and the reduction becomes
# invisible per method (SKY-12510 bounds labels; this list is static, not per-request).
POLICY_PACK_EVENT_METHODS = tuple(rule.method for rule in NOISY_EVENT_PACK_V1.rules)

__all__ = [
    "CONSOLE_BURST_PER_SECOND",
    "NOISY_EVENT_PACK_V1",
    "POLICY_PACK_EVENT_METHODS",
    "UNCONSUMED_EVENTS",
]
