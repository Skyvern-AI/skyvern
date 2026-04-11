import tiktoken

_ENCODING = tiktoken.encoding_for_model("gpt-4o")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))
