import pytest

from skyvern.forge.sdk.api.llm.utils import _coerce_response_to_dict


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"page_info": "Select country"}, ({"page_info": "Select country"}, False)),
        ([{"page_info": "First"}, {"page_info": "Second"}], ({"page_info": "First"}, False)),
        (["text", {"page_info": "First dict"}], ({"page_info": "First dict"}, False)),
        ([1, 2, 3], ({}, True)),
        ("not-a-dict", ({}, True)),
        ([], ({}, True)),
    ],
)
def test_coerce_response_to_dict_variants(response, expected):
    try:
        parsed = _coerce_response_to_dict(response)
        assert parsed == expected[0]
    except Exception:
        assert expected[1]
