"""Regression: the GET /credentials route is also called directly (not via FastAPI
dependency injection) by the scenario suite, e.g.
``credentials.get_credentials(org, page=1, page_size=10)``. When invoked that way,
unset parameters keep their ``Query(...)`` default *objects* rather than ``None``.

``search`` was previously forwarded raw, so it reached the repository as a truthy
``Query`` object and built a ``%<repr>%`` ILIKE pattern that matched no rows —
making ``get_credentials`` return an empty list (SKY-5679). Guard all three
optional filters so a direct call behaves like no filter.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from skyvern.forge.sdk.routes.credentials import get_credentials


def test_unset_query_params_are_passed_as_none() -> None:
    fake_org = MagicMock()
    fake_org.organization_id = "org_test"
    repo = AsyncMock(return_value=[])

    with patch("skyvern.forge.sdk.routes.credentials.app") as app_mock:
        app_mock.DATABASE.credentials.get_credentials = repo
        result = asyncio.run(get_credentials(current_org=fake_org, page=1, page_size=10))

    assert result == []
    repo.assert_awaited_once()
    assert repo.await_args.args[0] == "org_test"
    kwargs = repo.await_args.kwargs
    assert kwargs["search"] is None
    assert kwargs["credential_type"] is None
    assert kwargs["vault_type"] is None
    assert kwargs["folder_id"] is None
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10
