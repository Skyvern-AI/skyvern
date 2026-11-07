import random

from skyvern.utils.strings import RANDOM_STRING_POOL, generate_random_string


def test_generate_random_string_length_and_alphabet() -> None:
    for length in (1, 5, 32):
        value = generate_random_string(length)
        assert len(value) == length
        assert all(char in RANDOM_STRING_POOL for char in value)


def test_generate_random_string_does_not_reseed_global_random() -> None:
    """The implementation used to call ``random.seed()`` on every invocation,
    replacing the global PRNG state shared with the rest of the process."""
    random.seed(1234)
    expected = [random.random() for _ in range(8)]
    random.seed(1234)
    generate_random_string(5)
    assert [random.random() for _ in range(8)] == expected
