import hashlib


def calculate_sha256(data: str) -> str:
    """Helper function to calculate SHA256 hash of a string."""
    sha256_hash = hashlib.sha256()
    sha256_hash.update(data.encode())
    return sha256_hash.hexdigest()
