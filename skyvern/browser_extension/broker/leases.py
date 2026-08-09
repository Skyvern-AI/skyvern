from __future__ import annotations

from dataclasses import dataclass

DEFAULT_OFFER_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class OfferChange:
    client_id: str
    tab: dict


class LeaseTable:
    """Assigns each shared Chrome tab to at most one broker client.

    A client sees a tab only once the tab is leased to it or offered to it, so two agents never
    race for the same tab. Tabs an agent opens are leased to it outright; tabs the user shares by
    hand start unowned and are offered, one at a time, to an agent that currently holds nothing.
    """

    def __init__(self, offer_ttl_seconds: float = DEFAULT_OFFER_TTL_SECONDS) -> None:
        self._offer_ttl_seconds = offer_ttl_seconds
        self._tabs: dict[int, dict] = {}
        self._leases: dict[int, str] = {}
        self._offers: dict[int, str] = {}
        self._offer_deadlines: dict[int, float] = {}
        self._queue: list[str] = []

    def add_client(self, client_id: str) -> None:
        if client_id not in self._queue:
            self._queue.append(client_id)

    def remove_client(self, client_id: str) -> tuple[list[int], list[int]]:
        """Release everything the client held, as (leased, offered).

        Only the leased tabs were ever attached, so only those need detaching.
        """
        if client_id in self._queue:
            self._queue.remove(client_id)
        leased = [tab_id for tab_id, owner in self._leases.items() if owner == client_id]
        offered = [tab_id for tab_id, owner in self._offers.items() if owner == client_id]
        for tab_id in leased:
            del self._leases[tab_id]
        for tab_id in offered:
            self._release_offer(tab_id)
        return leased, offered

    def register_tab(self, tab: dict) -> None:
        tab_id = tab.get("tabId")
        if type(tab_id) is not int:
            return
        self._tabs[tab_id] = dict(tab)

    def forget_tab(self, tab_id: int) -> str | None:
        self._tabs.pop(tab_id, None)
        owner = self._leases.pop(tab_id, None) or self._offers.get(tab_id)
        self._release_offer(tab_id)
        return owner

    def reset(self, tabs: list[dict]) -> None:
        """Drop every lease and offer, then re-register the extension's current scope.

        Called when the extension reconnects: Chrome tore down every debugger attachment, so no
        client's claim on a tab survived.
        """
        self._tabs = {}
        self._leases = {}
        self._offers = {}
        self._offer_deadlines = {}
        for tab in tabs:
            self.register_tab(tab)

    def owner(self, tab_id: int) -> str | None:
        return self._leases.get(tab_id) or self._offers.get(tab_id)

    def lessee(self, tab_id: int) -> str | None:
        return self._leases.get(tab_id)

    def knows_tab(self, tab_id: int) -> bool:
        return tab_id in self._tabs

    def grant(self, tab_id: int, client_id: str, tab: dict | None = None) -> None:
        """Lease a tab to a client unconditionally — used for tabs that client just opened."""
        if tab is not None:
            self.register_tab(tab)
        elif tab_id not in self._tabs:
            self._tabs[tab_id] = {"tabId": tab_id, "url": "", "title": ""}
        self._release_offer(tab_id)
        self._leases[tab_id] = client_id

    def claim(self, tab_id: int, client_id: str) -> bool:
        """Convert this client's offer into a lease, or confirm a lease it already holds."""
        current = self._leases.get(tab_id)
        if current is not None:
            return current == client_id
        if self._offers.get(tab_id) != client_id:
            return False
        self._release_offer(tab_id)
        self._leases[tab_id] = client_id
        return True

    def release(self, tab_id: int, client_id: str) -> bool:
        if self._leases.get(tab_id) != client_id:
            return False
        del self._leases[tab_id]
        return True

    def visible_tabs(self, client_id: str) -> list[dict]:
        return [
            dict(tab)
            for tab_id, tab in self._tabs.items()
            if self._leases.get(tab_id) == client_id or self._offers.get(tab_id) == client_id
        ]

    def rotate(self, now: float) -> tuple[list[OfferChange], list[OfferChange]]:
        """Expire stale offers and hand unowned tabs to idle clients.

        Returns the offers granted and the offers revoked, so the caller can tell each client
        which tabs entered or left its scope.
        """
        revoked = [
            OfferChange(client_id=self._offers[tab_id], tab=dict(self._tabs.get(tab_id, {"tabId": tab_id})))
            for tab_id, deadline in list(self._offer_deadlines.items())
            if deadline <= now
        ]
        for change in revoked:
            self._release_offer(change.tab["tabId"])

        granted: list[OfferChange] = []
        for tab_id in list(self._tabs):
            if tab_id in self._leases or tab_id in self._offers:
                continue
            client_id = self._next_eligible_client()
            if client_id is None:
                break
            self._offers[tab_id] = client_id
            self._offer_deadlines[tab_id] = now + self._offer_ttl_seconds
            self._queue.remove(client_id)
            self._queue.append(client_id)
            granted.append(OfferChange(client_id=client_id, tab=dict(self._tabs[tab_id])))

        # When the only candidate is the client whose offer just lapsed, its view never changed.
        # Cancelling the pair keeps a lone idle agent from churning the tab out of scope and back.
        renewed = {_key(change) for change in granted} & {_key(change) for change in revoked}
        if not renewed:
            return granted, revoked
        return (
            [change for change in granted if _key(change) not in renewed],
            [change for change in revoked if _key(change) not in renewed],
        )

    def next_rotation_deadline(self) -> float | None:
        return min(self._offer_deadlines.values(), default=None)

    def _next_eligible_client(self) -> str | None:
        busy = set(self._leases.values()) | set(self._offers.values())
        return next((client_id for client_id in self._queue if client_id not in busy), None)

    def _release_offer(self, tab_id: int) -> None:
        self._offers.pop(tab_id, None)
        self._offer_deadlines.pop(tab_id, None)


def _key(change: OfferChange) -> tuple[str, int]:
    return change.client_id, change.tab["tabId"]
