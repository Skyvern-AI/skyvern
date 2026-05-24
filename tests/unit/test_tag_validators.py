import pytest

from skyvern.forge.sdk.workflow.models.validators import (
    RUN_METADATA_MAX_KEY_LENGTH,
    RUN_METADATA_MAX_KEYS,
    RUN_METADATA_MAX_VALUE_LENGTH,
    TAG_DESCRIPTION_MAX_LENGTH,
    normalize_run_metadata,
    normalize_tag_description,
    normalize_tags,
)


class TestNormalizeRunMetadataUnchanged:
    """Lock the existing normalize_run_metadata contract through the shared-core refactor."""

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
        # Only normalize_tags applies the stricter regex.
        assert normalize_run_metadata({"weird:key": "v"}) == {"weird:key": "v"}
        assert normalize_run_metadata({"k": "value,with,commas"}) == {"k": "value,with,commas"}


class TestNormalizeTagsCaps:
    def test_none_passes_through(self) -> None:
        assert normalize_tags(None) is None

    def test_strips_whitespace(self) -> None:
        assert normalize_tags({" env ": "  prod  "}) == {"env": "prod"}

    def test_skips_empty_pairs(self) -> None:
        assert normalize_tags({"a": "v", "b": "", "c": "  "}) == {"a": "v"}

    def test_over_max_keys_raises(self) -> None:
        too_many = {f"k{i}": "v" for i in range(RUN_METADATA_MAX_KEYS + 1)}
        with pytest.raises(ValueError, match="at most"):
            normalize_tags(too_many)

    def test_key_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="keys must be at most"):
            normalize_tags({"k" * (RUN_METADATA_MAX_KEY_LENGTH + 1): "v"})

    def test_value_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="values must be at most"):
            normalize_tags({"k": "v" * (RUN_METADATA_MAX_VALUE_LENGTH + 1)})


class TestNormalizeTagsKeyRegex:
    @pytest.mark.parametrize(
        "key",
        [
            "env",
            "customer.id",
            "team_name",
            "use-case",
            "Env",
            "k1",
            "a",
            "A1.b-c_d",
        ],
    )
    def test_valid_keys_accepted(self, key: str) -> None:
        assert normalize_tags({key: "v"}) == {key: "v"}

    @pytest.mark.parametrize(
        "key",
        [
            "key:with:colons",
            "key,with,commas",
            "key/with/slashes",
            "key?with?question",
            "key#hash",
            "key=equals",
            "key&amp",
            "key with space",
            "_leading_underscore",
            ".leading.dot",
            "-leading-dash",
        ],
    )
    def test_invalid_keys_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="must match"):
            normalize_tags({key: "v"})


class TestNormalizeTagsValueRestrictions:
    def test_comma_in_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain ','"):
            normalize_tags({"k": "value,with,comma"})

    def test_colon_in_value_allowed(self) -> None:
        # `:` is fine in values: the ?tags= parser splits on the first ':' only.
        assert normalize_tags({"env": "staging:us-east-1"}) == {"env": "staging:us-east-1"}

    def test_value_allows_other_punctuation(self) -> None:
        assert normalize_tags({"k": "value with spaces.and-dashes_and!special"}) == {
            "k": "value with spaces.and-dashes_and!special"
        }


class TestNormalizeTagsReservedNamespace:
    @pytest.mark.parametrize(
        "key",
        ["skyvern.trigger_type", "skyvern.managed", "skyvern.foo"],
    )
    def test_skyvern_prefix_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="reserved"):
            normalize_tags({key: "v"})

    def test_skyvern_inside_key_allowed(self) -> None:
        # Only the `skyvern.` prefix is reserved; substring is fine.
        assert normalize_tags({"my-skyvern": "v"}) == {"my-skyvern": "v"}

    def test_bare_skyvern_key_allowed(self) -> None:
        # 'skyvern' without the dot separator is not the reserved prefix.
        assert normalize_tags({"skyvern": "v"}) == {"skyvern": "v"}


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
