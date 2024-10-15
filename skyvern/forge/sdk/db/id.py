import hashlib
import itertools
import os
import platform
import random
import time

# 6/20/2022 12AM
BASE_EPOCH = 1655683200
VERSION = 0

# Number of bits
TIMESTAMP_BITS = 32
WORKER_ID_BITS = 21
SEQUENCE_BITS = 10
VERSION_BITS = 1

# Bit shits (left)
TIMESTAMP_SHIFT = 32
WORKER_ID_SHIFT = 11
SEQUENCE_SHIFT = 1
VERSION_SHIFT = 0

SEQUENCE_MAX = (2**SEQUENCE_BITS) - 1
_sequence_start = None
SEQUENCE_COUNTER = itertools.count()
_worker_hash = None

# prefix
ORGANIZATION_AUTH_TOKEN_PREFIX = "oat"
ORG_PREFIX = "o"
TASK_PREFIX = "tsk"
USER_PREFIX = "u"
STEP_PREFIX = "stp"
ARTIFACT_PREFIX = "a"
WORKFLOW_PREFIX = "w"
WORKFLOW_PERMANENT_ID_PREFIX = "wpid"
WORKFLOW_RUN_PREFIX = "wr"
WORKFLOW_PARAMETER_PREFIX = "wp"
AWS_SECRET_PARAMETER_PREFIX = "asp"
OUTPUT_PARAMETER_PREFIX = "op"
BITWARDEN_LOGIN_CREDENTIAL_PARAMETER_PREFIX = "blc"
BITWARDEN_SENSITIVE_INFORMATION_PARAMETER_PREFIX = "bsi"
BITWARDEN_CREDIT_CARD_DATA_PARAMETER_PREFIX = "bccd"
TASK_GENERATION_PREFIX = "tg"


def generate_workflow_id() -> str:
    int_id = generate_id()
    return f"{WORKFLOW_PREFIX}_{int_id}"


def generate_workflow_permanent_id() -> str:
    int_id = generate_id()
    return f"{WORKFLOW_PERMANENT_ID_PREFIX}_{int_id}"


def generate_workflow_run_id() -> str:
    int_id = generate_id()
    return f"{WORKFLOW_RUN_PREFIX}_{int_id}"


def generate_aws_secret_parameter_id() -> str:
    int_id = generate_id()
    return f"{AWS_SECRET_PARAMETER_PREFIX}_{int_id}"


def generate_workflow_parameter_id() -> str:
    int_id = generate_id()
    return f"{WORKFLOW_PARAMETER_PREFIX}_{int_id}"


def generate_output_parameter_id() -> str:
    int_id = generate_id()
    return f"{OUTPUT_PARAMETER_PREFIX}_{int_id}"


def generate_bitwarden_login_credential_parameter_id() -> str:
    int_id = generate_id()
    return f"{BITWARDEN_LOGIN_CREDENTIAL_PARAMETER_PREFIX}_{int_id}"


def generate_bitwarden_sensitive_information_parameter_id() -> str:
    int_id = generate_id()
    return f"{BITWARDEN_SENSITIVE_INFORMATION_PARAMETER_PREFIX}_{int_id}"


def generate_bitwarden_credit_card_data_parameter_id() -> str:
    int_id = generate_id()
    return f"{BITWARDEN_CREDIT_CARD_DATA_PARAMETER_PREFIX}_{int_id}"


def generate_organization_auth_token_id() -> str:
    int_id = generate_id()
    return f"{ORGANIZATION_AUTH_TOKEN_PREFIX}_{int_id}"


def generate_org_id() -> str:
    int_id = generate_id()
    return f"{ORG_PREFIX}_{int_id}"


def generate_task_id() -> str:
    int_id = generate_id()
    return f"{TASK_PREFIX}_{int_id}"


def generate_step_id() -> str:
    int_id = generate_id()
    return f"{STEP_PREFIX}_{int_id}"


def generate_artifact_id() -> str:
    int_id = generate_id()
    return f"{ARTIFACT_PREFIX}_{int_id}"


def generate_user_id() -> str:
    int_id = generate_id()
    return f"{USER_PREFIX}_{int_id}"


def generate_task_generation_id() -> str:
    int_id = generate_id()
    return f"{TASK_GENERATION_PREFIX}_{int_id}"


def generate_totp_code_id() -> str:
    int_id = generate_id()
    return f"totp_{int_id}"


def generate_action_id() -> str:
    int_id = generate_id()
    return f"a_{int_id}"


def generate_id() -> int:
    """
    generate a 64-bit int ID
    """
    create_at = current_time() - BASE_EPOCH
    sequence = _increment_and_get_sequence()

    time_part = _mask_shift(create_at, TIMESTAMP_BITS, TIMESTAMP_SHIFT)
    worker_part = _mask_shift(_get_worker_hash(), WORKER_ID_BITS, WORKER_ID_SHIFT)
    sequence_part = _mask_shift(sequence, SEQUENCE_BITS, SEQUENCE_SHIFT)
    version_part = _mask_shift(VERSION, VERSION_BITS, VERSION_SHIFT)

    return time_part | worker_part | sequence_part | version_part


def _increment_and_get_sequence() -> int:
    global _sequence_start
    if _sequence_start is None:
        _sequence_start = random.randint(0, SEQUENCE_MAX)

    return (_sequence_start + next(SEQUENCE_COUNTER)) % SEQUENCE_MAX


def current_time() -> int:
    return int(time.time())


def current_time_ms() -> int:
    return int(time.time() * 1000)


def _mask_shift(value: int, mask_bits: int, shift_bits: int) -> int:
    return (value & ((2**mask_bits) - 1)) << shift_bits


def _get_worker_hash() -> int:
    global _worker_hash
    if _worker_hash is None:
        _worker_hash = _generate_worker_hash()
    return _worker_hash


def _generate_worker_hash() -> int:
    worker_identity = f"{platform.node()}:{os.getpid()}"
    return int(hashlib.md5(worker_identity.encode()).hexdigest()[-15:], 16)
