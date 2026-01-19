import tiktoken


def count_tokens(text: str) -> int:
    """
    tiktoken sends a request to https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken to get the token
    """
    try:
        return len(tiktoken.encoding_for_model("gpt-4o").encode(text))
    except Exception:
        return 0
