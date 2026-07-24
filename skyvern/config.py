import logging
import os
import platform
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from skyvern import constants
from skyvern.constants import REPO_ROOT_DIR, SKYVERN_DIR
from skyvern.utils.env_paths import (
    BACKEND_ENV_BASENAMES,
    BACKEND_ENV_INTENT_ENV_VAR,
    EnvIntent,
    backend_env_path_candidates,
)


def _default_database_string() -> str:
    """Return the default DATABASE_STRING.

    Uses a SQLite file at ~/.skyvern/data.db so that ``skyvern run server``
    works out of the box without Docker or Postgres.  Users who set
    DATABASE_STRING in .env or the environment get Postgres automatically
    (pydantic-settings reads env before the default_factory runs).

    This is a pure string computation — no filesystem side effects.
    The parent directory is created by _ensure_sqlite_dir() at engine
    build time (agent_db.py) or server bootstrap time (api_app.py).
    """
    db_path = Path.home() / ".skyvern" / "data.db"
    return f"sqlite+aiosqlite:///{db_path}"


def _ensure_sqlite_dir(database_string: str) -> None:
    """Create the parent directory for a file-backed SQLite database URL.

    No-op for in-memory SQLite (`:memory:`) or non-SQLite URLs.
    """
    if not database_string.startswith("sqlite") or ":memory:" in database_string:
        return
    db_file = database_string.split("///", 1)[-1]
    Path(db_file).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


# NOTE: _DEFAULT_ENV_FILES resolves .env paths at import time and assumes
# the process has changed dir to the desired project root by this time.
# Even if we were to resolve paths at instantiation time, the global `settings`
# singleton instantiation at the bottom of this file also runs at import time
# and relies on the same assumption.
#
# pydantic-settings applies later dotenv files with higher precedence, so the
# resolver's read-priority order is reversed here. With no explicit CLI intent,
# AUTO preserves legacy self-hosted imports by only considering ./.env.
def _settings_env_intent() -> EnvIntent:
    try:
        return EnvIntent(os.getenv(BACKEND_ENV_INTENT_ENV_VAR, EnvIntent.AUTO.value))
    except ValueError:
        return EnvIntent.AUTO


def _settings_env_file_candidates(basename: str) -> tuple[Path, ...]:
    return tuple(reversed(backend_env_path_candidates(basename, intent=_settings_env_intent())))


_DEFAULT_ENV_FILES = tuple(
    candidate for basename in BACKEND_ENV_BASENAMES for candidate in _settings_env_file_candidates(basename)
)


LOG = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_DEFAULT_ENV_FILES, extra="ignore")

    # settings for experimentation
    ENABLE_EXP_ALL_TEXTUAL_ELEMENTS_INTERACTABLE: bool = False

    # Script reviewer settings
    SCRIPT_REVIEW_DAILY_CAP: int = 5  # Max script reviews per wpid per day (all review types)
    SELF_HEAL_DAILY_CAP: int = 5

    ADDITIONAL_MODULES: list[str] = []

    BROWSER_TYPE: str = "chromium-headful"
    BROWSER_REMOTE_DEBUGGING_URL: str = "http://127.0.0.1:9222"
    BROWSER_REMOTE_DEBUGGING_HOST_HEADER: str | None = None
    BROWSER_REMOTE_DEBUGGING_CONNECT_HEADERS: str | None = None
    BROWSER_CDP_CONNECT_TIMEOUT_MS: int = 120000
    # connect_over_cdp_with_retry budget. The defaults give ~15s of total backoff
    # (1+2+3+4+5) across attempts so a browser that is slow to bind its local CDP
    # port (e.g. a cold-starting stealth Chromium on 127.0.0.1:9222) is reconnected
    # instead of surfacing an opaque ECONNREFUSED.
    CDP_CONNECT_RETRY_ATTEMPTS: int = 6
    CDP_CONNECT_RETRY_BACKOFF_SECONDS: list[float] = [1, 2, 3, 4, 5]
    CHROME_EXECUTABLE_PATH: str | None = None
    MAX_SCRAPING_RETRIES: int = 0
    VIDEO_PATH: str | None = "./video"
    VIDEO_COMPRESSION_ENABLED: bool = True
    VIDEO_COMPRESSION_CRF: int = 28
    VIDEO_COMPRESSION_PRESET: str = "veryfast"
    VIDEO_COMPRESSION_TIMEOUT_SECONDS: float = 300.0
    VIDEO_FINAL_SYNC_TIMEOUT_SECONDS: float = 750.0
    HAR_PATH: str | None = "./har"
    LOG_PATH: str = "./log"
    TEMP_PATH: str = "./temp"
    DOWNLOAD_PATH: str = f"{REPO_ROOT_DIR}/downloads"
    BROWSER_ACTION_TIMEOUT_MS: int = 5000
    BROWSER_ACTION_MAX_EXECUTION_SECONDS: int = 1200
    POPUP_VIDEO_PATH_TIMEOUT_SECONDS: float = 3.0
    CACHED_ACTION_DELAY_SECONDS: float = 1.0
    # Page readiness settings for cached action execution
    # These help prevent cached actions from executing before the page is fully loaded
    PAGE_READY_NETWORK_IDLE_TIMEOUT_MS: float = 3000  # Wait for network idle (short timeout)
    PAGE_READY_LOADING_INDICATOR_TIMEOUT_MS: float = 5000  # Wait for loading indicators to disappear
    PAGE_READY_DOM_STABLE_MS: float = 300  # Time with no DOM mutations to consider stable
    PAGE_READY_DOM_STABILITY_TIMEOUT_MS: float = 3000  # Max time to wait for DOM stability
    BROWSER_SCREENSHOT_TIMEOUT_MS: int = 20000
    BROWSER_LOADING_TIMEOUT_MS: int = 60000
    # Pre-screenshot readiness guard; kept short so a page that never settles
    # degrades fast instead of burning the full loading-timeout budget.
    BROWSER_SCREENSHOT_LOAD_STATE_TIMEOUT_MS: int = 5000
    BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS: int = 60 * 1000  # 1 minute
    CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS: int = 300
    # In-block OTP email/SMS poll budget; bounded under CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS
    # so one fetch can't consume the whole block. TOTP re-mint is instant and unaffected.
    CODE_BLOCK_OTP_POLL_TIMEOUT_SECONDS: int = 120
    OPTION_LOADING_TIMEOUT_MS: int = 600000
    MAX_STEPS_PER_RUN: int = 10
    MAX_STEPS_PER_TASK_V2: int = 25
    MAX_ITERATIONS_PER_TASK_V2: int = 50
    # Upper bound on the number of open tabs screenshotted at task_v2 completion so the
    # trajectory judge can verify "keep N tabs open" rubrics without unbounded artifact spend.
    MAX_COMPLETION_TAB_SCREENSHOTS_PER_TASK_V2: int = 20
    # Overall wall-clock budget for the completion screenshot loop.
    COMPLETION_TAB_SCREENSHOTS_TOTAL_TIMEOUT_SECONDS: float = 60.0
    MAX_NUM_SCREENSHOTS: int = 10
    # Emit per-call image_tokens/image_cost/image_count on the LLM duration log so
    # screenshot spend can be monitored independently of the provider's blended tokens.
    LLM_IMAGE_COST_TRACKING_ENABLED: bool = True
    # Ratio should be between 0 and 1.
    # If the task has been running for more steps than this ratio of the max steps per run, then we'll log a warning.
    LONG_RUNNING_TASK_WARNING_RATIO: float = 0.95
    MAX_RETRIES_PER_STEP: int = 5
    # Static kill-switch for fail-fast shadow observability. Per-org rollout is the
    # PostHog flag FAIL_FAST_SHADOW; this only force-enables it everywhere (local/testing).
    FAIL_FAST_SHADOW: bool = False
    # Default-off telemetry for deterministic submission signals; enabling it only schedules
    # fire-and-forget shadow evaluation and does not change run behavior.
    SKYVERN_SUBMISSION_SIGNAL_SHADOW: bool = False
    # Global kill-switch for select/autocomplete shadow-match observability (LLM-vs-deterministic
    # agreement logging). Not per-org; set false to silence the logs everywhere.
    SKYVERN_SELECT_SHADOW_MATCH: bool = True
    FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS: float = Field(default=0, ge=0, le=60)
    DEBUG_MODE: bool = False
    DATABASE_STRING: str = Field(default_factory=_default_database_string)
    DATABASE_REPLICA_STRING: str | None = None
    DATABASE_STATEMENT_TIMEOUT_MS: int = 60000
    DISABLE_CONNECTION_POOL: bool = False
    DATABASE_POOL_SIZE: int = 5
    DATABASE_POOL_MAX_OVERFLOW: int = 10
    # Timeout/recycle defaults mirror SQLAlchemy's QueuePool. Size pools per service
    # via env vars: raising defaults here multiplies across every engine and replica
    # and can exhaust pgbouncer client connections.
    DATABASE_POOL_TIMEOUT: int = 30
    DATABASE_POOL_RECYCLE: int = -1
    PROMPT_ACTION_HISTORY_WINDOW: int = 1
    TASK_RESPONSE_ACTION_SCREENSHOT_COUNT: int = 3

    ENV: str = "local"
    BROWSER_STREAMING_MODE: str = "vnc"
    EXECUTE_ALL_STEPS: bool = True
    JSON_LOGGING: bool = False
    LOG_RAW_API_REQUESTS: bool = True
    # Successful (<400) GET/HEAD/OPTIONS are skipped by default: they dominate
    # log volume (health checks, polling) while carrying no mutation to audit.
    LOG_RAW_API_REQUESTS_SUCCESSFUL_READS: bool = False
    LOG_LEVEL: str = "INFO"
    # Opt-in INFO-log sampling for high-volume orgs. A log call marked
    # sampling=True is dropped from stdout/Datadog with probability
    # (1 - LOG_SAMPLING_RATE) when its org is in LOG_SAMPLING_ORG_IDS. The full
    # line is still captured in the per-run S3 log artifact. Both defaults make
    # this a no-op: an empty org list samples nothing and rate 1.0 keeps all.
    LOG_SAMPLING_RATE: float = 1.0
    LOG_SAMPLING_ORG_IDS: list[str] = []
    COPILOT_REQUEST_POLICY_CLASSIFIER_TIMEOUT_SECONDS: float = 12.0
    COPILOT_TURN_INTENT_CLASSIFIER_TIMEOUT_SECONDS: float = 12.0
    COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS: float = 12.0
    # Consecutive repair runs that make no newly-verified forward progress before the
    # copilot stops re-running and escalates honestly. Set very high to disable the ceiling.
    COPILOT_REPAIR_CEILING_CONSECUTIVE_IDENTICAL: int = 3
    COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS: float = 4.0
    # Bounded settle-then-re-perceive after a non-advancing click on a precondition-gated control:
    # re-probe the side-effect-free extractor a few times (hard-capped) until a just-issued AJAX populates.
    COPILOT_CLICK_SETTLE_MAX_PROBES: int = 3
    COPILOT_CLICK_SETTLE_DELAY_SECONDS: float = 0.6
    COPILOT_CLICK_SETTLE_DEADLINE_SECONDS: float = 3.5
    # Kill switch for the clickable-controls grounding channel: when off, composition evidence omits the
    # clickable_controls key entirely, reverting both the re-perception attach and the evaluate steer.
    COPILOT_CLICK_REPERCEPTION_ATTACH_ENABLED: bool = True
    # Staged rollout for treating omitted runtime workflow proxy values as direct/no-proxy.
    # Off preserves the historical implicit residential default for anti-bot-sensitive traffic.
    RUNTIME_PROXY_DEFAULT_NONE_ENABLED: bool = False
    # Dispatch flag for the workflow copilot v2 (openai-agents-SDK rewrite).
    # Off = existing direct-LLM copilot at workflow_copilot_chat_post.
    # On = new agent-SDK path under skyvern.forge.sdk.copilot.
    # Per-environment canary; default off until we are confident.
    ENABLE_WORKFLOW_COPILOT_V2: bool = False
    # Experimental Workflow Copilot v2 branch mode.
    # Off = standard block authoring. On = prefer code blocks for browser work.
    WORKFLOW_COPILOT_CODE_BLOCK_MODE: bool = False
    WORKFLOW_COPILOT_TERMINAL_ENVELOPE_RENDER: bool = False
    WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY: bool = False
    WORKFLOW_COPILOT_QA_TOKEN_BUDGET: int | None = Field(default=None, gt=0)
    # Pause a BUILD turn in place on a typed mid-loop credential ask instead of ending it;
    # the FE resumes the same turn via a credential-connect card. Off = today's turn-terminal behavior.
    # Requires app.CACHE to be a shared cache (Redis) -- a same-process-only cache can't
    # coordinate the poller with a /credential-response POST that may land on another worker,
    # so this is a guaranteed no-op behind app.CACHE.is_shared regardless of this flag.
    WORKFLOW_COPILOT_CREDENTIAL_PAUSE_ENABLED: bool = True
    WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS: int = 300
    # Kill switch for the live codegen-progress SSE frame (drafted block labels while an authoring
    # tool call streams). Off restores exact pre-change behavior; old frontends drop the frame either way.
    WORKFLOW_COPILOT_CODEGEN_PROGRESS_ENABLED: bool = True
    # Default code_only for MCP block/workflow tools. Off = permissive.
    MCP_CODE_ONLY_MODE: bool = False
    # Default for the bounded code-block self-heal; off by default.
    ENABLE_CODE_BLOCK_SELF_HEALING: bool = False
    SELF_HEAL_MAX_ACTIONS: int = 15
    SELF_HEAL_WALL_CLOCK_BUDGET_SECONDS: int = 300
    PORT: int = 8000
    ALLOWED_ORIGINS: list[str] = ["*"]
    ALLOWED_ORIGIN_REGEX: str | None = None
    BLOCKED_HOSTS: list[str] = ["localhost"]
    ALLOWED_HOSTS: list[str] = []
    # SFTP uploads connect directly from the worker, so private/internal hosts are
    # blocked by default; self-hosted deployments with internal SFTP targets can enable.
    ALLOW_SFTP_INTERNAL_HOSTS: bool = False

    # Secret key for JWT. Please generate your own secret key in production
    SECRET_KEY: str = "PLACEHOLDER"
    # Algorithm used to sign the JWT
    SIGNATURE_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # one week

    # Artifact storage settings
    ARTIFACT_STORAGE_PATH: str = f"{SKYVERN_DIR}/artifacts"

    # Supported storage types: local, s3cloud, azureblob
    SKYVERN_STORAGE_TYPE: str = "local"

    # Shared Redis URL (used by any service that needs Redis)
    REDIS_URL: str = "redis://localhost:6379/0"

    # S3/AWS settings
    AWS_REGION: str = "us-east-1"
    MAX_UPLOAD_FILE_SIZE: int = 10 * 1024 * 1024  # 10 MB
    MAX_HTTP_DOWNLOAD_FILE_SIZE: int = 500 * 1024 * 1024  # 500 MB
    PRESIGNED_URL_EXPIRATION: int = 60 * 60 * 24  # 24 hours
    AWS_S3_BUCKET_ARTIFACTS: str = "skyvern-artifacts"
    AWS_S3_BUCKET_SCREENSHOTS: str = "skyvern-screenshots"
    AWS_S3_BUCKET_BROWSER_SESSIONS: str = "skyvern-browser-sessions"
    AWS_S3_BUCKET_UPLOADS: str = "skyvern-uploads"

    # Azure Blob Storage settings
    AZURE_STORAGE_ACCOUNT_NAME: str | None = None
    AZURE_STORAGE_ACCOUNT_KEY: str | None = None
    AZURE_STORAGE_CONTAINER_ARTIFACTS: str = "skyvern-artifacts"
    AZURE_STORAGE_CONTAINER_SCREENSHOTS: str = "skyvern-screenshots"
    AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS: str = "skyvern-browser-sessions"
    AZURE_STORAGE_CONTAINER_UPLOADS: str = "skyvern-uploads"

    # Google Cloud Storage settings (bucket names are globally unique — override per deployment)
    GCS_PROJECT_ID: str | None = None
    GCS_BUCKET_ARTIFACTS: str = "skyvern-artifacts"
    GCS_BUCKET_SCREENSHOTS: str = "skyvern-screenshots"
    GCS_BUCKET_BROWSER_SESSIONS: str = "skyvern-browser-sessions"
    GCS_BUCKET_UPLOADS: str = "skyvern-uploads"
    # GSA email used to sign V4 URLs when running under Workload Identity (no local private key).
    GCS_SIGNER_SA_EMAIL: str | None = None

    SKYVERN_TELEMETRY: bool = True
    ANALYTICS_ID: str = "anonymous"
    ANALYTICS_TEST_ID: str | None = None
    POSTHOG_PROJECT_API_KEY: str = "phc_bVT2ugnZhMHRWqMvSRHPdeTjaPxQqT3QSsI3r5FlQR5"
    POSTHOG_PROJECT_HOST: str = "https://app.posthog.com"
    MCP_POSTHOG_PROJECT_API_KEY: str | None = "phc_m4epBbGS1Hf4NPRFNpR4WQ9Ob6yGy6SLbQckBxp3n0P"
    MCP_POSTHOG_PROJECT_HOST: str = "https://app.posthog.com"

    # email settings
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 25
    SMTP_USERNAME: str = "username"
    SMTP_PASSWORD: str = "password"

    # browser settings
    BROWSER_LOCALE: str | None = None  # "en-US"
    BROWSER_TIMEZONE: str = "America/New_York"
    # Directory containing pre-built default browser profiles ({dir}/chrome/ and {dir}/chromium/).
    # When set, used as the default profile source for new browser sessions.
    # Cloud workers download S3 profiles here at startup; self-hosted users can point this at a
    # local profile directory. Leave empty to use versioned temp caches and clean empty fallbacks.
    DEFAULT_BROWSER_PROFILE_DIR: str = ""
    BROWSER_WIDTH: int = 1920
    BROWSER_HEIGHT: int = 1080
    # Playwright's ffmpeg encoder runs continuously while the browser is open and its CPU cost
    # scales with pixel count. Unset means Playwright's default (viewport scaled to fit 800x800);
    # set both to record at an explicit resolution.
    BROWSER_RECORDING_WIDTH: int | None = None
    BROWSER_RECORDING_HEIGHT: int | None = None
    # Max concurrent LLM enrichment calls per live browser-recording interpretation
    # session. Bounds the per-action enrichment fan-out so a burst of interactions
    # can't flood the event loop with simultaneous LLM requests.
    RECORDING_ENRICHMENT_MAX_CONCURRENCY: int = 4
    # LLM used to enrich live recording draft steps (label/title/goal). A fast, cheap
    # model keeps the click->labeled-draft latency low. Falls back to the default
    # LLM_API_HANDLER when the key is unset or not registered in this environment.
    RECORDING_ENRICHMENT_LLM_KEY: str = "GEMINI_3.1_FLASH_LITE"
    # Server-side kill switch for delta interpretation updates. Deltas are also
    # gated per-connection on the client declaring support (begin-exfiltration
    # supports_interpretation_deltas); this only force-disables them everywhere.
    RECORDING_INTERPRETATION_DELTAS_ENABLED: bool = True
    BROWSER_POLICY_FILE: str = "/etc/chromium/policies/managed/policies.json"
    BROWSER_LOGS_ENABLED: bool = True
    BROWSER_CURSOR_VISUALIZATION: bool = False
    BROWSER_MAX_PAGES_NUMBER: int = 10
    BROWSER_ADDITIONAL_ARGS: list[str] = []

    # Add extension folders name here to load extension in your browser
    EXTENSIONS_BASE_PATH: str = "./extensions"
    EXTENSIONS: list[str] = []

    # Workflow constant parameters
    WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY: str = "SKYVERN_DOWNLOAD_DIRECTORY"
    WORKFLOW_TEMPLATING_STRICTNESS: str = "lax"  # options: "strict", "lax"
    WORKFLOW_WAIT_BLOCK_MAX_SEC: int = 30 * 60

    # Saved browser session settings
    BROWSER_SESSION_BASE_PATH: str = f"{constants.REPO_ROOT_DIR}/browser_sessions"

    #####################
    # Bitwarden Configs #
    #####################
    BITWARDEN_TIMEOUT_SECONDS: int = 60
    BITWARDEN_MAX_RETRIES: int = 2
    BITWARDEN_MAX_JITTER_SECONDS: float = 2.0

    # task generation settings
    PROMPT_CACHE_WINDOW_HOURS: int = 24

    #####################
    # LLM Configuration #
    #####################
    # ACTIVE LLM PROVIDER
    LLM_KEY: str = "OPENAI_GPT5_5"  # This is the model name
    LLM_API_KEY: str | None = None  # API key for the model
    SECONDARY_LLM_KEY: str | None = None
    SELECT_AGENT_LLM_KEY: str | None = None
    NORMAL_SELECT_AGENT_LLM_KEY: str | None = None
    CUSTOM_SELECT_AGENT_LLM_KEY: str | None = None
    SINGLE_CLICK_AGENT_LLM_KEY: str | None = None
    SINGLE_INPUT_AGENT_LLM_KEY: str | None = None
    PROMPT_BLOCK_LLM_KEY: str | None = None
    PARSE_SELECT_LLM_KEY: str | None = None
    EXTRACTION_LLM_KEY: str | None = None
    CHECK_USER_GOAL_LLM_KEY: str | None = None
    AUTO_COMPLETION_LLM_KEY: str | None = None
    SCRIPT_GENERATION_LLM_KEY: str | None = None
    SCRIPT_REVIEWER_LLM_KEY: str | None = None
    ADAPTIVE_SCRIPT_GEN_LLM_KEY: str | None = None
    WORKFLOW_COPILOT_LLM_KEY: str | None = None
    WORKFLOW_COPILOT_AGENT_LLM_KEY: str | None = None
    WORKFLOW_COPILOT_FAST_LLM_KEY: str | None = None
    WORKFLOW_COPILOT_LITE_LLM_KEY: str | None = None
    # COMMON
    LLM_CONFIG_TIMEOUT: int = 300
    LLM_CONFIG_MAX_TOKENS: int = 4096
    LLM_CONFIG_TEMPERATURE: float = 0
    LLM_CONFIG_SUPPORT_VISION: bool = True  # Whether the model supports vision
    LLM_CONFIG_ADD_ASSISTANT_PREFIX: bool = False  # Whether to add assistant prefix
    # Self-hosted users commonly run Ollama on localhost/private networks. Cloud
    # overrides this to False so user-defined LLM API bases use SSRF protections.
    ALLOW_CUSTOM_LLM_LOCAL_API_BASES: bool = True
    # LLM PROVIDER SPECIFIC
    ENABLE_OPENAI: bool = False
    ENABLE_ANTHROPIC: bool = False
    ENABLE_BEDROCK_ANTHROPIC: bool = False
    ENABLE_AZURE: bool = False
    ENABLE_AZURE_GPT4O_MINI: bool = False
    ENABLE_AZURE_O3_MINI: bool = False
    ENABLE_BEDROCK: bool = False
    ENABLE_GEMINI: bool = False
    ENABLE_VERTEX_AI: bool = False
    ENABLE_AZURE_CUA: bool = False
    ENABLE_OPENAI_COMPATIBLE: bool = False
    # OPENAI
    OPENAI_API_KEY: str | None = None
    GPT5_REASONING_EFFORT: str | None = "medium"
    OPENAI_CUA_MODEL: str = "computer-use-preview"
    # ANTHROPIC
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_CUA_LLM_KEY: str = "ANTHROPIC_CLAUDE4.6_SONNET"

    # VOLCENGINE (Doubao)
    ENABLE_VOLCENGINE: bool = False
    VOLCENGINE_API_KEY: str | None = None
    VOLCENGINE_API_BASE: str = "https://ark.cn-beijing.volces.com/api/v3"
    VOLCENGINE_CUA_LLM_KEY: str = "VOLCENGINE_DOUBAO_1_5_THINKING_VISION_PRO"

    # Yutori Navigator
    ENABLE_YUTORI: bool = False
    YUTORI_API_KEY: str | None = None
    YUTORI_API_BASE: str = "https://api.yutori.com/v1"
    YUTORI_MODEL: str = "n1.5-latest"
    YUTORI_LLM_KEY: str = "YUTORI_NAVIGATOR"
    YUTORI_TOOL_SET: str = ""

    # OPENAI COMPATIBLE
    OPENAI_COMPATIBLE_MODEL_NAME: str | None = None
    OPENAI_COMPATIBLE_API_KEY: str | None = None
    OPENAI_COMPATIBLE_API_BASE: str | None = None
    OPENAI_COMPATIBLE_API_VERSION: str | None = None
    OPENAI_COMPATIBLE_MAX_TOKENS: int | None = None
    OPENAI_COMPATIBLE_TEMPERATURE: float | None = None
    OPENAI_COMPATIBLE_SUPPORTS_VISION: bool = False
    OPENAI_COMPATIBLE_ADD_ASSISTANT_PREFIX: bool = False
    OPENAI_COMPATIBLE_MODEL_KEY: str = "OPENAI_COMPATIBLE"
    OPENAI_COMPATIBLE_REASONING_EFFORT: str | None = None
    OPENAI_COMPATIBLE_GITHUB_COPILOT_DOMAIN: str = "githubcopilot.com"

    # AZURE
    AZURE_DEPLOYMENT: str | None = None
    AZURE_API_KEY: str | None = None
    AZURE_API_BASE: str | None = None
    AZURE_API_VERSION: str | None = None
    AZURE_CUA_API_KEY: str | None = None
    AZURE_CUA_ENDPOINT: str | None = None
    AZURE_CUA_DEPLOYMENT: str | None = "computer-use-preview"
    AZURE_CUA_API_VERSION: str | None = "2025-03-01-preview"

    # AZURE GPT-4o mini
    AZURE_GPT4O_MINI_DEPLOYMENT: str | None = None
    AZURE_GPT4O_MINI_API_KEY: str | None = None
    AZURE_GPT4O_MINI_API_BASE: str | None = None
    AZURE_GPT4O_MINI_API_VERSION: str | None = None

    # AZURE o3 mini
    AZURE_O3_MINI_DEPLOYMENT: str | None = None
    AZURE_O3_MINI_API_KEY: str | None = None
    AZURE_O3_MINI_API_BASE: str | None = None
    AZURE_O3_MINI_API_VERSION: str | None = None

    # AZURE gpt-4.1
    ENABLE_AZURE_GPT4_1: bool = False
    AZURE_GPT4_1_DEPLOYMENT: str = "gpt-4.1"
    AZURE_GPT4_1_API_KEY: str | None = None
    AZURE_GPT4_1_API_BASE: str | None = None
    AZURE_GPT4_1_API_VERSION: str = "2025-01-01-preview"

    # AZURE gpt-4.1 mini
    ENABLE_AZURE_GPT4_1_MINI: bool = False
    AZURE_GPT4_1_MINI_DEPLOYMENT: str = "gpt-4.1-mini"
    AZURE_GPT4_1_MINI_API_KEY: str | None = None
    AZURE_GPT4_1_MINI_API_BASE: str | None = None
    AZURE_GPT4_1_MINI_API_VERSION: str = "2025-01-01-preview"

    # AZURE gpt-4.1 nano
    ENABLE_AZURE_GPT4_1_NANO: bool = False
    AZURE_GPT4_1_NANO_DEPLOYMENT: str = "gpt-4.1-nano"
    AZURE_GPT4_1_NANO_API_KEY: str | None = None
    AZURE_GPT4_1_NANO_API_BASE: str | None = None
    AZURE_GPT4_1_NANO_API_VERSION: str = "2025-01-01-preview"

    # AZURE o4-mini
    ENABLE_AZURE_O4_MINI: bool = False
    AZURE_O4_MINI_DEPLOYMENT: str = "o4-mini"
    AZURE_O4_MINI_API_KEY: str | None = None
    AZURE_O4_MINI_API_BASE: str | None = None
    AZURE_O4_MINI_API_VERSION: str = "2025-01-01-preview"

    # AZURE o3
    ENABLE_AZURE_O3: bool = False
    AZURE_O3_DEPLOYMENT: str = "o3"
    AZURE_O3_API_KEY: str | None = None
    AZURE_O3_API_BASE: str | None = None
    AZURE_O3_API_VERSION: str = "2025-01-01-preview"

    # AZURE gpt-5
    ENABLE_AZURE_GPT5: bool = False
    AZURE_GPT5_DEPLOYMENT: str = "gpt-5"
    AZURE_GPT5_API_KEY: str | None = None
    AZURE_GPT5_API_BASE: str | None = None
    AZURE_GPT5_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5 mini
    ENABLE_AZURE_GPT5_MINI: bool = False
    AZURE_GPT5_MINI_DEPLOYMENT: str = "gpt-5-mini"
    AZURE_GPT5_MINI_API_KEY: str | None = None
    AZURE_GPT5_MINI_API_BASE: str | None = None
    AZURE_GPT5_MINI_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5 nano
    ENABLE_AZURE_GPT5_NANO: bool = False
    AZURE_GPT5_NANO_DEPLOYMENT: str = "gpt-5-nano"
    AZURE_GPT5_NANO_API_KEY: str | None = None
    AZURE_GPT5_NANO_API_BASE: str | None = None
    AZURE_GPT5_NANO_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5.1
    ENABLE_AZURE_GPT5_1: bool = False
    AZURE_GPT5_1_DEPLOYMENT: str = "gpt-5.1"
    AZURE_GPT5_1_API_KEY: str | None = None
    AZURE_GPT5_1_API_BASE: str | None = None
    AZURE_GPT5_1_API_VERSION: str = "2025-04-01-preview"
    # AZURE gpt-5.2
    ENABLE_AZURE_GPT5_2: bool = False
    AZURE_GPT5_2_DEPLOYMENT: str = "gpt-5.2"
    AZURE_GPT5_2_API_KEY: str | None = None
    AZURE_GPT5_2_API_BASE: str | None = None
    AZURE_GPT5_2_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5.4
    ENABLE_AZURE_GPT5_4: bool = False
    AZURE_GPT5_4_DEPLOYMENT: str = "gpt-5.4"
    AZURE_GPT5_4_API_KEY: str | None = None
    AZURE_GPT5_4_API_BASE: str | None = None
    AZURE_GPT5_4_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5.6 sol
    ENABLE_AZURE_GPT5_6_SOL: bool = False
    AZURE_GPT5_6_SOL_DEPLOYMENT: str = "gpt-5.6-sol"
    AZURE_GPT5_6_SOL_API_KEY: str | None = None
    AZURE_GPT5_6_SOL_API_BASE: str | None = None
    AZURE_GPT5_6_SOL_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5.6 terra
    ENABLE_AZURE_GPT5_6_TERRA: bool = False
    AZURE_GPT5_6_TERRA_DEPLOYMENT: str = "gpt-5.6-terra"
    AZURE_GPT5_6_TERRA_API_KEY: str | None = None
    AZURE_GPT5_6_TERRA_API_BASE: str | None = None
    AZURE_GPT5_6_TERRA_API_VERSION: str = "2025-04-01-preview"

    # AZURE gpt-5.6 luna
    ENABLE_AZURE_GPT5_6_LUNA: bool = False
    AZURE_GPT5_6_LUNA_DEPLOYMENT: str = "gpt-5.6-luna"
    AZURE_GPT5_6_LUNA_API_KEY: str | None = None
    AZURE_GPT5_6_LUNA_API_BASE: str | None = None
    AZURE_GPT5_6_LUNA_API_VERSION: str = "2025-04-01-preview"

    # GEMINI
    GEMINI_API_KEY: str | None = None
    GEMINI_INCLUDE_THOUGHT: bool = False
    GEMINI_THINKING_BUDGET: int | None = None
    DEFAULT_THINKING_BUDGET: int = 1024
    EXTRACT_ACTION_THINKING_BUDGET: int = 512

    # VERTEX_AI
    VERTEX_CREDENTIALS: str | None = None
    VERTEX_PROJECT_ID: str | None = None
    VERTEX_LOCATION: str | None = None

    # NOVITA AI
    ENABLE_NOVITA: bool = False
    NOVITA_API_KEY: str | None = None
    NOVITA_API_VERSION: str = "v3"

    # OLLAMA
    ENABLE_OLLAMA: bool = False
    OLLAMA_SERVER_URL: str | None = None
    OLLAMA_MODEL: str | None = None
    OLLAMA_SUPPORTS_VISION: bool = False

    # OPENROUTER
    ENABLE_OPENROUTER: bool = False
    OPENROUTER_API_KEY: str | None = None
    OPENROUTER_MODEL: str | None = None
    OPENROUTER_API_BASE: str = "https://openrouter.ai/api/v1"

    # GROQ
    ENABLE_GROQ: bool = False
    GROQ_API_KEY: str | None = None
    GROQ_MODEL: str | None = None
    GROQ_API_BASE: str = "https://api.groq.com/openai/v1"

    # MOONSHOT AI
    ENABLE_MOONSHOT: bool = False
    MOONSHOT_API_KEY: str | None = None
    MOONSHOT_API_BASE: str = "https://api.moonshot.cn/v1"

    # INCEPTION AI
    ENABLE_INCEPTION: bool = False
    INCEPTION_API_KEY: str | None = None
    INCEPTION_API_BASE: str = "https://api.inceptionlabs.ai/v1"

    # TOTP Settings
    TOTP_LIFESPAN_MINUTES: int = 10
    VERIFICATION_CODE_INITIAL_WAIT_TIME_SECS: int = 40
    VERIFICATION_CODE_POLLING_TIMEOUT_MINS: int = 15

    # Bitwarden Settings
    BITWARDEN_CLIENT_ID: str | None = None
    BITWARDEN_CLIENT_SECRET: str | None = None
    BITWARDEN_MASTER_PASSWORD: str | None = None
    BITWARDEN_EMAIL: str | None = None
    OP_SERVICE_ACCOUNT_TOKEN: str | None = None

    # Where credentials are stored: skyvern, bitwarden, azure_vault, gcp, or custom
    CREDENTIAL_VAULT_TYPE: str = "bitwarden"
    ENABLE_LOCAL_CREDENTIAL_VAULT: bool | None = None
    LOCAL_CREDENTIAL_VAULT_PATH: str = str(Path.home() / ".skyvern" / "credential_vault")
    LOCAL_CREDENTIAL_VAULT_KEY: str | None = None

    # GCP Secret Manager credential vault settings
    GCP_CREDENTIAL_VAULT_PROJECT_ID: str | None = None  # project hosting the Secret Manager secrets
    GCP_CREDENTIAL_VAULT_PREFIX: str = "skyvern-cred-"  # secret-id prefix; must be unique per deployment

    # Azure Setting
    AZURE_TENANT_ID: str | None = None
    AZURE_CLIENT_ID: str | None = None
    AZURE_CLIENT_SECRET: str | None = None
    # The Azure Key Vault name to store credentials
    AZURE_CREDENTIAL_VAULT: str | None = None

    # Custom Credential Service Settings
    CUSTOM_CREDENTIAL_API_BASE_URL: str | None = None
    CUSTOM_CREDENTIAL_API_TOKEN: str | None = None

    # Skyvern Auth Bitwarden Settings
    SKYVERN_AUTH_BITWARDEN_CLIENT_ID: str | None = None
    SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET: str | None = None
    SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD: str | None = None
    SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID: str | None = None

    BITWARDEN_SERVER: str = "http://localhost"
    BITWARDEN_SERVER_PORT: int = 8002

    SVG_MAX_LENGTH: int = 100000
    SVG_MAX_PARSING_ELEMENT_CNT: int = 3000
    ENABLE_CSS_SVG_PARSING: bool = True

    ENABLE_LOG_ARTIFACTS: bool = False
    ENABLE_CODE_BLOCK: bool = True

    TASK_BLOCKED_SITE_FALLBACK_URL: str = "https://www.google.com"

    SKYVERN_APP_URL: str = "http://localhost:8080"
    # SkyvernClient Settings
    SKYVERN_BASE_URL: str = "https://api.skyvern.com"
    SKYVERN_API_KEY: str = "PLACEHOLDER"

    SKYVERN_BROWSER_VNC_PORT: int = 6080
    """
    The websockified port on which the VNC server of a persistent browser is
    listening.
    """

    PYLON_IDENTITY_VERIFICATION_SECRET: str | None = None
    """
    The secret used to sign the email/identity of the user.
    """

    ARTIFACT_CONTENT_HMAC_KEYRING: str | None = None
    """
    JSON keyring for HMAC-SHA256 signing of bundled-artifact content URLs.

    When set, /artifacts/{id}/content URLs generated for bundled artifacts carry
    expiry/kid/sig query parameters and the endpoint validates them without requiring
    an org-level API key.

    Format::

        {
          "current_kid": "2026-03-12-v1",
          "keys": {
            "2026-03-12-v1": {
              "secret": "my-hmac-secret",
              "created_at": "2026-03-12"
            }
          }
        }

    current_kid must be present in keys.

    Key rotation: add the new key to keys and point current_kid at it; keep the
    old key in keys until all URLs it signed have expired (12 h), then remove it.
    """

    # Debug Session Settings
    DEBUG_SESSION_TIMEOUT_MINUTES: int = 20
    """
    The timeout for a persistent browser session backing a debug session,
    in minutes.
    """

    DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES: int = 10
    """
    Threshold for browser session timeout extension.
    - V1 (OSS): extends when remaining >= threshold, raises if below (expired).
    - V2 (cloud): extends when remaining <= threshold, no-ops if above (plenty of time).
    Set to 10 minutes so that a 5-minute renewal loop gets 2+ attempts before expiry.
    """

    PERSISTENT_SESSIONS_REAPER_INTERVAL_SECONDS: int = 60
    """
    How often the OSS in-process reaper scans for persistent browser sessions past their
    timeout and closes them, freeing the leaked Chromium + record_video ffmpeg encoder.
    Set to 0 to disable the reaper.
    """

    ENCRYPTOR_AES_SECRET_KEY: str = "fillmein"
    ENCRYPTOR_AES_SALT: str | None = None
    ENCRYPTOR_AES_IV: str | None = None
    ENABLE_ENCRYPTION: bool = False

    # Google OAuth settings (used by the Google Sheets connector)
    GOOGLE_OAUTH_CLIENT_ID: str | None = None
    GOOGLE_OAUTH_CLIENT_SECRET: str | None = None
    # Hostnames allowed as the OAuth ``redirect_uri`` sent to Google. Defense-in-depth
    # alongside Google's own redirect_uri allowlist (which is the real enforcement
    # gate — it validates the full URI against its registered list). This setting is
    # intentionally host-only: any path or port on an approved host passes. Empty
    # list means no redirect_uri may be supplied at all (the route layer rejects
    # with 400) when CLIENT_ID is set.
    GOOGLE_OAUTH_REDIRECT_HOSTS: list[str] = Field(default_factory=list)
    # Origins allowed as the bounce-back destination after OAuth callback.
    # Never sent to Google. Two entry shapes — port handling differs:
    #   - Exact-match: ``https://host:port`` matches that exact origin (port included).
    #     ``https://app.example.com`` does NOT match ``https://app.example.com:8443``.
    #   - Suffix wildcard: ``*.foo.com`` matches any HTTPS hostname ending in ``.foo.com``
    #     regardless of port (so preview deploys on non-default ports work). Rejects
    #     bare-suffix spoofs like ``attacker-foo.com``.
    # Fails closed: an empty list rejects every app_origin, so self-hosted operators
    # who want to use the bounce-back flow must populate this with at least one entry.
    GOOGLE_OAUTH_APP_ORIGINS: list[str] = Field(default_factory=list)
    # OSS/self-hosted instances can store the Google OAuth client config per org
    # through Settings. Skyvern Cloud keeps the centrally managed OAuth client.
    ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG: bool = True

    MICROSOFT_OAUTH_CLIENT_ID: str | None = None
    MICROSOFT_OAUTH_CLIENT_SECRET: str | None = None
    MICROSOFT_OAUTH_TENANT: str = "common"
    MICROSOFT_OAUTH_REDIRECT_HOSTS: list[str] = Field(default_factory=list)
    MICROSOFT_OAUTH_APP_ORIGINS: list[str] = Field(default_factory=list)

    # Google Sheets API runtime tuning
    GOOGLE_SHEETS_API_TIMEOUT_SECONDS: float = 30.0
    GOOGLE_SHEETS_API_MAX_RETRIES: int = 3
    # Google Drive API runtime tuning
    GOOGLE_DRIVE_API_TIMEOUT_SECONDS: float = 30.0
    GOOGLE_DRIVE_API_MAX_RETRIES: int = 3

    # Cleanup Cron Settings
    ENABLE_CLEANUP_CRON: bool = False
    """Enable periodic cleanup of temporary data (temp files and stale processes)."""
    CLEANUP_CRON_INTERVAL_MINUTES: int = 10
    """Interval in minutes for the cleanup cron job."""
    CLEANUP_STALE_TASK_THRESHOLD_HOURS: int = 24
    """Tasks/workflows not updated for this many hours are considered stale (stuck)."""

    TEMP_ARTIFACT_SWEEP_MAX_AGE_HOURS: float = 48.0
    """Age gate (hours) for the always-on sweep of per-run LOG_PATH/DOWNLOAD_PATH dirs left behind on
    crash paths. Non-positive disables the sweep."""

    # Workflow Schedule Settings
    ENABLE_WORKFLOW_SCHEDULES: bool = True
    """Enable recurring workflow schedules in the OSS/local server."""
    WORKFLOW_SCHEDULE_POLL_INTERVAL_SECONDS: float = 60.0
    """How often the OSS/local scheduler scans for due workflow schedules."""
    WORKFLOW_SCHEDULE_MAX_CONCURRENT_RUNS: int = 1
    """Maximum number of scheduled workflow runs dispatched concurrently by one OSS server process."""

    # OpenTelemetry Settings
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "skyvern"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_METRICS_ENABLED: bool = True
    OTEL_LOGS_ENABLED: bool = True
    OTEL_EXPORTER_INSECURE: bool = True
    # Log level for the OTLP gRPC exporter's own logger. Raise above WARNING (e.g.
    # "CRITICAL") to drop its retry/failure records where the OTLP endpoint is
    # intentionally unavailable; the default keeps export failures visible.
    OTEL_EXPORTER_LOG_LEVEL: str = "WARNING"
    # Per-export deadline (seconds) for the OTLP span exporter. Must exceed the exporter's
    # ~31s retry-backoff window (2**n over _MAX_RETRYS) so a brief node-local collector blip
    # is retried to success instead of logged as a failure; the library default (10s) cuts
    # the retry sequence short and turns each blip into an error burst.
    OTEL_EXPORTER_TIMEOUT_SECONDS: float = Field(
        default=45.0,
        gt=0,
        validation_alias=AliasChoices(
            "OTEL_EXPORTER_TIMEOUT_SECONDS",
            "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
            "OTEL_EXPORTER_OTLP_TIMEOUT",
        ),
    )
    # BatchSpanProcessor queue depth (library default 2048); enlarged to buffer more spans
    # while an export is retrying against a briefly unavailable collector.
    OTEL_BSP_MAX_QUEUE_SIZE: int = 8192

    # script generation settings
    WORKFLOW_START_BLOCK_LABEL: str = "__start_block__"

    def get_model_name_to_llm_key(self, organization_id: str | None = None) -> dict[str, dict[str, str]]:
        """
        Keys are model names available to blocks in the frontend. These map to key names
        in LLMConfigRegistry._configs.
        """
        mapping: dict[str, dict[str, str]] = {}

        # Gemini models: prefer Vertex when enabled, fall back to direct Gemini API
        gemini_models = [
            ("gemini-2.5-pro-preview-05-06", "VERTEX_GEMINI_2.5_PRO", "GEMINI_2.5_PRO", "Gemini 2.5 Pro"),
            ("gemini-2.5-flash", "VERTEX_GEMINI_2.5_FLASH", "GEMINI_2.5_FLASH", "Gemini 2.5 Flash"),
            ("gemini-3-pro-preview", "VERTEX_GEMINI_3_PRO", "GEMINI_3_PRO", "Gemini 3 Pro (Latest)"),
            ("gemini-3.0-flash", "VERTEX_GEMINI_3.0_FLASH", "GEMINI_3.0_FLASH", "Gemini 3 Flash"),
            ("gemini-3.5-flash", "VERTEX_GEMINI_3.5_FLASH", "GEMINI_3.5_FLASH", "Gemini 3.5 Flash"),
            ("gemini-3.5-flash-lite", "VERTEX_GEMINI_3.5_FLASH_LITE", "GEMINI_3.5_FLASH_LITE", "Gemini 3.5 Flash Lite"),
            ("gemini-3.6-flash", "VERTEX_GEMINI_3.6_FLASH", "GEMINI_3.6_FLASH", "Gemini 3.6 Flash"),
        ]
        for model_name, vertex_key, gemini_key, label in gemini_models:
            mapping[model_name] = {
                "llm_key": vertex_key if self.ENABLE_VERTEX_AI else gemini_key,
                "label": label,
            }

        # Gemini Flash Lite: Vertex-only (no direct Gemini API config exists)
        mapping["gemini-2.5-flash-lite"] = {
            "llm_key": "VERTEX_GEMINI_2.5_FLASH_LITE",
            "label": "Gemini 2.5 Flash Lite",
        }

        mapping["mercury-2"] = {"llm_key": "INCEPTION_MERCURY_2", "label": "Inception Mercury 2"}

        # Their configs are registered only under ENABLE_OPENROUTER, so without it the dropdown
        # would offer models that resolve to unregistered configs and fail at runtime.
        if self.ENABLE_OPENROUTER:
            mapping["deepseek-v4-flash"] = {"llm_key": "OPENROUTER_DEEPSEEK_V4_FLASH", "label": "DeepSeek V4 Flash"}
            mapping["mimo-v2.5"] = {"llm_key": "OPENROUTER_XIAOMI_MIMO_V2_5", "label": "Xiaomi MiMo V2.5"}

        # GPT models: prefer Azure when enabled, fall back to OpenAI
        gpt_models = [
            ("azure/gpt-4.1", self.ENABLE_AZURE_GPT4_1, "AZURE_OPENAI_GPT4_1", "OPENAI_GPT4_1", "GPT 4.1"),
            ("azure/gpt-5", self.ENABLE_AZURE_GPT5, "AZURE_OPENAI_GPT5", "OPENAI_GPT5", "GPT 5"),
            (
                "azure/gpt-5-mini",
                self.ENABLE_AZURE_GPT5_MINI,
                "AZURE_OPENAI_GPT5_MINI",
                "OPENAI_GPT5_MINI",
                "GPT 5 Mini",
            ),
            ("azure/gpt-5.2", self.ENABLE_AZURE_GPT5_2, "AZURE_OPENAI_GPT5_2", "OPENAI_GPT5_2", "GPT 5.2"),
            ("azure/gpt-5.4", self.ENABLE_AZURE_GPT5_4, "AZURE_OPENAI_GPT5_4", "OPENAI_GPT5_4", "GPT 5.4"),
            (
                "azure/gpt-5.6-sol",
                self.ENABLE_AZURE_GPT5_6_SOL,
                "AZURE_OPENAI_GPT5_6_SOL",
                "OPENAI_GPT5_6_SOL",
                "GPT 5.6 Sol",
            ),
            (
                "azure/gpt-5.6-terra",
                self.ENABLE_AZURE_GPT5_6_TERRA,
                "AZURE_OPENAI_GPT5_6_TERRA",
                "OPENAI_GPT5_6_TERRA",
                "GPT 5.6 Terra",
            ),
            (
                "azure/gpt-5.6-luna",
                self.ENABLE_AZURE_GPT5_6_LUNA,
                "AZURE_OPENAI_GPT5_6_LUNA",
                "OPENAI_GPT5_6_LUNA",
                "GPT 5.6 Luna",
            ),
            ("azure/o3", self.ENABLE_AZURE_O3, "AZURE_OPENAI_O3", "OPENAI_O3", "GPT O3"),
        ]
        for model_name, azure_enabled, azure_key, openai_key, label in gpt_models:
            mapping[model_name] = {"llm_key": azure_key if azure_enabled else openai_key, "label": label}

        # Anthropic models: prefer Bedrock when enabled, fall back to direct API
        if self.ENABLE_BEDROCK_ANTHROPIC:
            mapping["us.anthropic.claude-opus-4-20250514-v1:0"] = {
                "llm_key": "BEDROCK_ANTHROPIC_CLAUDE4_OPUS_INFERENCE_PROFILE",
                "label": "Anthropic Claude 4 Opus",
            }
            mapping["us.anthropic.claude-sonnet-4-20250514-v1:0"] = {
                "llm_key": "BEDROCK_ANTHROPIC_CLAUDE4_SONNET_INFERENCE_PROFILE",
                "label": "Anthropic Claude 4 Sonnet",
            }
        else:
            mapping["us.anthropic.claude-opus-4-20250514-v1:0"] = {
                "llm_key": "ANTHROPIC_CLAUDE4_OPUS",
                "label": "Anthropic Claude 4 Opus",
            }
            mapping["us.anthropic.claude-sonnet-4-20250514-v1:0"] = {
                "llm_key": "ANTHROPIC_CLAUDE4_SONNET",
                "label": "Anthropic Claude 4 Sonnet",
            }

        mapping["claude-haiku-4-5-20251001"] = {
            "llm_key": "ANTHROPIC_CLAUDE4.5_HAIKU",
            "label": "Anthropic Claude 4.5 Haiku",
        }

        # Anthropic Claude 4.5 Sonnet & Opus
        if self.ENABLE_BEDROCK_ANTHROPIC:
            mapping["claude-sonnet-4-5-20250929"] = {
                "llm_key": "BEDROCK_ANTHROPIC_CLAUDE4.5_SONNET_INFERENCE_PROFILE",
                "label": "Anthropic Claude 4.5 Sonnet",
            }
            mapping["claude-opus-4-5-20251101"] = {
                "llm_key": "BEDROCK_ANTHROPIC_CLAUDE4.5_OPUS_INFERENCE_PROFILE",
                "label": "Anthropic Claude 4.5 Opus",
            }
        else:
            mapping["claude-sonnet-4-5-20250929"] = {
                "llm_key": "ANTHROPIC_CLAUDE4.5_SONNET",
                "label": "Anthropic Claude 4.5 Sonnet",
            }
            mapping["claude-opus-4-5-20251101"] = {
                "llm_key": "ANTHROPIC_CLAUDE4.5_OPUS",
                "label": "Anthropic Claude 4.5 Opus",
            }

        # Anthropic Claude 4.6 Opus: prefer Bedrock when enabled, fall back to direct API
        if self.ENABLE_BEDROCK_ANTHROPIC:
            mapping["claude-opus-4-6"] = {
                "llm_key": "BEDROCK_ANTHROPIC_CLAUDE4.6_OPUS_INFERENCE_PROFILE",
                "label": "Anthropic Claude 4.6 Opus",
            }
        else:
            mapping["claude-opus-4-6"] = {
                "llm_key": "ANTHROPIC_CLAUDE4.6_OPUS",
                "label": "Anthropic Claude 4.6 Opus",
            }

        # Anthropic Claude Fable 5: prefer Bedrock when enabled, fall back to direct API
        if self.ENABLE_BEDROCK_ANTHROPIC:
            mapping["claude-fable-5"] = {
                "llm_key": "BEDROCK_ANTHROPIC_CLAUDE5_FABLE_INFERENCE_PROFILE",
                "label": "Anthropic Claude Fable 5",
            }
        else:
            mapping["claude-fable-5"] = {
                "llm_key": "ANTHROPIC_CLAUDE5_FABLE",
                "label": "Anthropic Claude Fable 5",
            }

        try:
            from skyvern.forge.sdk.api.llm.custom_llm_registry import (  # noqa: PLC0415
                get_custom_llm_model_mappings,
            )

            mapping.update(get_custom_llm_model_mappings(organization_id=organization_id))
        except Exception:
            # Settings is used by scripts and import-time paths before the API app is fully initialized.
            pass

        return mapping

    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        super().model_post_init(__context)
        if platform.system() != "Windows":
            return

        scheme, sep, remainder = self.DATABASE_STRING.partition("://")
        if not sep:
            return

        dialect, driver_sep, driver = scheme.partition("+")
        if not driver_sep or driver not in {"psycopg", "psycopg2"}:
            return

        updated_string = f"{dialect}+asyncpg://{remainder}"
        if updated_string == self.DATABASE_STRING:
            return

        LOG.warning(
            "Detected Windows environment: switching DATABASE_STRING driver from psycopg to asyncpg "
            "for compatibility with the Proactor event loop policy."
        )
        object.__setattr__(self, "DATABASE_STRING", updated_string)

    def is_sqlite(self) -> bool:
        return self.DATABASE_STRING.startswith("sqlite")

    def is_cloud_environment(self) -> bool:
        """
        :return: True if env is not local, else False
        """
        return self.ENV != "local"

    def is_local_credential_vault_enabled(self) -> bool:
        if self.ENABLE_LOCAL_CREDENTIAL_VAULT is not None:
            return self.ENABLE_LOCAL_CREDENTIAL_VAULT
        return not self.is_cloud_environment()

    def execute_all_steps(self) -> bool:
        """
        This provides the functionality to execute steps one by one through the local UI.
        ***Value is always True if ENV is not local.***

        :return: True if env is not local, else the value of EXECUTE_ALL_STEPS
        """
        if self.is_cloud_environment():
            return True
        else:
            return self.EXECUTE_ALL_STEPS


settings = Settings()
