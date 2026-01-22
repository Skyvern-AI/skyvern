import os
import random
import string
import uuid

RANDOM_STRING_POOL = string.ascii_letters + string.digits


def generate_random_string(length: int = 5) -> str:
    # Use the os.urandom(16) as the seed
    random.seed(os.urandom(16))
    return "".join(random.choices(RANDOM_STRING_POOL, k=length))


def is_uuid(string: str) -> bool:
    try:
        uuid.UUID(string)
        return True
    except ValueError:
        return False
