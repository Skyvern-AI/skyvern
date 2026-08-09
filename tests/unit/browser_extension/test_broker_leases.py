from __future__ import annotations

from skyvern.browser_extension.broker.leases import LeaseTable


def tab(tab_id: int, url: str = "https://example.test") -> dict:
    return {"tabId": tab_id, "url": url, "title": ""}


def test_a_tab_an_agent_opened_is_leased_to_it_alone() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.add_client("b")

    leases.grant(1, "a", tab(1))

    assert leases.lessee(1) == "a"
    assert leases.visible_tabs("a") == [tab(1)]
    assert leases.visible_tabs("b") == []
    assert not leases.claim(1, "b")
    assert leases.claim(1, "a")


def test_an_unowned_tab_is_offered_to_exactly_one_idle_client() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.add_client("b")
    leases.register_tab(tab(1))

    granted, revoked = leases.rotate(now=0.0)

    assert revoked == []
    assert [change.client_id for change in granted] == ["a"]
    assert leases.visible_tabs("a") == [tab(1)]
    assert leases.visible_tabs("b") == []


def test_an_offer_nobody_claims_moves_on_to_the_next_client() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.add_client("b")
    leases.register_tab(tab(1))

    first, _ = leases.rotate(now=0.0)
    assert [change.client_id for change in first] == ["a"]

    second, revoked = leases.rotate(now=30.0)
    assert [change.client_id for change in revoked] == ["a"]
    assert [change.client_id for change in second] == ["b"]
    assert leases.visible_tabs("a") == []
    assert leases.visible_tabs("b") == [tab(1)]


def test_a_lone_idle_client_keeps_its_offer_instead_of_churning() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.register_tab(tab(1))
    leases.rotate(now=0.0)

    granted, revoked = leases.rotate(now=30.0)

    assert granted == []
    assert revoked == []
    assert leases.owner(1) == "a"


def test_claiming_an_offer_turns_it_into_a_lease_that_does_not_expire() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.register_tab(tab(1))
    leases.rotate(now=0.0)

    assert leases.claim(1, "a")

    granted, revoked = leases.rotate(now=10_000.0)
    assert granted == []
    assert revoked == []
    assert leases.lessee(1) == "a"


def test_a_busy_client_is_skipped_when_another_shared_tab_appears() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.grant(1, "a", tab(1))
    leases.register_tab(tab(2))

    granted, _ = leases.rotate(now=0.0)

    assert granted == []
    assert leases.owner(2) is None


def test_disconnecting_reports_leases_and_offers_separately_so_only_leases_are_detached() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.add_client("b")
    leases.grant(1, "a", tab(1))
    leases.grant(2, "a", tab(2))
    leases.register_tab(tab(3))
    leases.rotate(now=0.0)
    assert leases.owner(3) == "b"

    assert leases.remove_client("a") == ([1, 2], [])
    assert leases.remove_client("b") == ([], [3])
    assert all(leases.owner(tab_id) is None for tab_id in (1, 2, 3))


def test_forgetting_a_tab_reports_whoever_held_it() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.grant(1, "a", tab(1))

    assert leases.forget_tab(1) == "a"
    assert leases.forget_tab(1) is None
    assert leases.visible_tabs("a") == []


def test_an_extension_reconnect_voids_every_claim() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.add_client("b")
    leases.grant(1, "a", tab(1))
    leases.grant(2, "b", tab(2))

    leases.reset([tab(1), tab(3)])

    assert leases.owner(1) is None
    assert leases.owner(2) is None
    assert leases.knows_tab(3)
    assert leases.visible_tabs("a") == []
    assert leases.visible_tabs("b") == []


def test_release_only_drops_the_holder_s_own_lease() -> None:
    leases = LeaseTable()
    leases.add_client("a")
    leases.grant(1, "a", tab(1))

    assert not leases.release(1, "b")
    assert leases.lessee(1) == "a"
    assert leases.release(1, "a")
    assert leases.lessee(1) is None
