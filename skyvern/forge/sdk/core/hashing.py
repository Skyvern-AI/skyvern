import hashlib


def generate_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()
