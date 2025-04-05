import tiktoken


def count_tokens(text: str) -> int:
    return len(tiktoken.encoding_for_model("gpt-4o").encode(text))
