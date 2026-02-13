import pytest
from freezegun import freeze_time

from skyvern.forge.sdk.core.security import create_access_token, generate_skyvern_webhook_signature


@pytest.mark.skip(reason="Skipping test_generate_skyvern_signature")
@freeze_time("2023-11-30 00:00:00")
def test_generate_skyvern_signature() -> None:
    api_key = create_access_token("o_12345")
    payload = {"task_id": "t_12345", "float": 1.0}
    signed_data = generate_skyvern_webhook_signature(payload, api_key)
    assert signed_data.signature == "1fac4204e1abc7cb0bdf1a42eb17d27f6f1feba065d5726777d5eb77581298c1"
