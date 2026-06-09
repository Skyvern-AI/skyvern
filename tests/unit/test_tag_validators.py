import pytest

from skyvern.forge.sdk.workflow.models.validators import (
    RUN_METADATA_MAX_KEY_LENGTH,
    RUN_METADATA_MAX_KEYS,
    RUN_METADATA_MAX_VALUE_LENGTH,
    TAG_DESCRIPTION_MAX_LENGTH,
    TAG_KEY_MAX_LENGTH,
    TAG_VALUE_MAX_LENGTH,
    normalize_optional_tag_key,
    normalize_optional_tag_value,
    normalize_run_metadata,
    normalize_tag_description,
    normalize_tag_value,
)


class TestNormalizeRunMetadataUnchanged:
    """Lock the existing normalize_run_metadata contract through the tag refactor."""

    def test_none_passes_through(self) -> None:
        assert normalize_run_metadata(None) is None

    def test_strips_whitespace(self) -> None:
        assert normalize_run_metadata({" customer ": "  acme  "}) == {"customer": "acme"}

    def test_skips_empty_values(self) -> None:
        assert normalize_run_metadata({"a": "v", "b": "", "c": "   "}) == {"a": "v"}

    def test_all_empty_returns_none(self) -> None:
        assert normalize_run_metadata({"": "", "  ": "  "}) is None

    def test_over_max_keys_raises(self) -> None:
        too_many = {f"k{i}": "v" for i in range(RUN_METADATA_MAX_KEYS + 1)}
        with pytest.raises(ValueError, match="at most"):
            normalize_run_metadata(too_many)

    def test_key_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="keys must be at most"):
            normalize_run_metadata({"k" * (RUN_METADATA_MAX_KEY_LENGTH + 1): "v"})

    def test_value_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="values must be at most"):
            normalize_run_metadata({"k": "v" * (RUN_METADATA_MAX_VALUE_LENGTH + 1)})

    def test_run_metadata_permits_punctuation_in_keys(self) -> None:
        # Existing shipped behavior: run_metadata does NOT restrict key characters.
        # Only the tag validators apply the stricter regex.
        assert normalize_run_metadata({"weird:key": "v"}) == {"weird:key": "v"}
        assert normalize_run_metadata({"k": "value,with,commas"}) == {"k": "value,with,commas"}


class TestNormalizeOptionalTagKey:
    def test_none_is_standalone(self) -> None:
        # Null/blank key == standalone label (no group).
        assert normalize_optional_tag_key(None) is None
        assert normalize_optional_tag_key("") is None
        assert normalize_optional_tag_key("   ") is None

    def test_strips_whitespace(self) -> None:
        assert normalize_optional_tag_key(" env ") == "env"

    def test_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            normalize_optional_tag_key(1)

    def test_key_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="keys must be at most"):
            normalize_optional_tag_key("k" * (TAG_KEY_MAX_LENGTH + 1))

    @pytest.mark.parametrize(
        "key",
        ["env", "customer.id", "team_name", "use-case", "Env", "k1", "a", "A1.b-c_d"],
    )
    def test_valid_keys_accepted(self, key: str) -> None:
        assert normalize_optional_tag_key(key) == key

    @pytest.mark.parametrize(
        "key",
        [
            "key:with:colons",
            "key,with,commas",
            "key/with/slashes",
            "key with space",
            "_leading_underscore",
            ".leading.dot",
            "-leading-dash",
        ],
    )
    def test_invalid_keys_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="must match"):
            normalize_optional_tag_key(key)

    @pytest.mark.parametrize("key", ["skyvern.trigger_type", "skyvern.managed", "skyvern.foo"])
    def test_skyvern_prefix_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="reserved"):
            normalize_optional_tag_key(key)

    def test_skyvern_inside_key_allowed(self) -> None:
        assert normalize_optional_tag_key("my-skyvern") == "my-skyvern"

    def test_bare_skyvern_key_allowed(self) -> None:
        # 'skyvern' without the dot separator is not the reserved prefix.
        assert normalize_optional_tag_key("skyvern") == "skyvern"


class TestNormalizeTagValue:
    def test_strips_whitespace(self) -> None:
        assert normalize_tag_value("  prod  ") == "prod"

    def test_empty_raises(self) -> None:
        # The label (value) is always required.
        with pytest.raises(ValueError, match="required"):
            normalize_tag_value("   ")

    def test_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            normalize_tag_value(1)

    def test_value_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="values must be at most"):
            normalize_tag_value("v" * (TAG_VALUE_MAX_LENGTH + 1))

    def test_comma_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain ','"):
            normalize_tag_value("value,with,comma")

    def test_colon_allowed(self) -> None:
        # `:` is fine in values: the ?tags= parser splits on the first ':' only.
        assert normalize_tag_value("staging:us-east-1") == "staging:us-east-1"

    def test_value_allows_other_punctuation(self) -> None:
        assert normalize_tag_value("value with spaces.and-dashes_and!special") == (
            "value with spaces.and-dashes_and!special"
        )


class TestNormalizeOptionalTagValue:
    def test_none_and_blank_pass_through(self) -> None:
        assert normalize_optional_tag_value(None) is None
        assert normalize_optional_tag_value("") is None
        assert normalize_optional_tag_value("   ") is None

    def test_valid_value_trimmed(self) -> None:
        assert normalize_optional_tag_value("  prod ") == "prod"

    def test_comma_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain ','"):
            normalize_optional_tag_value("a,b")


class TestNormalizeTagDescription:
    def test_none_passes_through(self) -> None:
        assert normalize_tag_description(None) is None

    def test_strips_whitespace(self) -> None:
        assert normalize_tag_description("  hello  ") == "hello"

    def test_empty_after_strip_returns_none(self) -> None:
        assert normalize_tag_description("   ") is None

    def test_over_max_length_raises(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            normalize_tag_description("x" * (TAG_DESCRIPTION_MAX_LENGTH + 1))
