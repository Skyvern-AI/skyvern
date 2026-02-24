import logging
import platform
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from skyvern import constants
from skyvern.constants import REPO_ROOT_DIR, SKYVERN_DIR
from skyvern.utils.env_paths import resolve_backend_env_path

# NOTE: _DEFAULT_ENV_FILES resolves .env paths at import time and assumes
# the process has changed dir to the desired project root by this time.
# Even if we were to resolve paths at instantiation time, the global `settings`
# singleton instantiation at the bottom of this file also runs at import time
# and relies on the same assumption.
_DEFAULT_ENV_FILES = (
    resolve_backend_env_path(".env"),
    resolve_backend_env_path(".env.staging"),
    resolve_backend_env_path(".env.prod"),
)


LOG = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_DEFAULT_ENV_FILES, extra="ignore")

    # settings for experimentation
    ENABLE_EXP_ALL_TEXTUAL_ELEMENTS_INTERACTABLE: bool = False

    ADDITIONAL_MODULES: list[str] = []

    BROWSER_TYPE: str = "chromium-headful"
    BROWSER_REMOTE_DEBUGGING_URL: str = "http://127.0.0.1:9222"
    CHROME_EXECUTABLE_PATH: str | None = None
    MAX_SCRAPING_RETRIES: int = 0
    VIDEO_PATH: str | None = "./video"
    HAR_PATH: str | None = "./har"
    LOG_PATH: str = "./log"
    TEMP_PATH: str = "./temp"
    DOWNLOAD_PATH: str = f"{REPO_ROOT_DIR}/downloads"
    BROWSER_ACTION_TIMEOUT_MS: int = 5000
    CACHED_ACTION_DELAY_SECONDS: float = 1.0
    # Page readiness settings for cached action execution
    # These help prevent cached actions from executing before the page is fully loaded
    PAGE_READY_NETWORK_IDLE_TIMEOUT_MS: float = 3000  # Wait for network idle (short timeout)
    PAGE_READY_LOADING_INDICATOR_TIMEOUT_MS: float = 5000  # Wait for loading indicators to disappear
    PAGE_READY_DOM_STABLE_MS: float = 300  # Time with no DOM mutations to consider stable
    PAGE_READY_DOM_STABILITY_TIMEOUT_MS: float = 3000  # Max time to wait for DOM stability
    BROWSER_SCREENSHOT_TIMEOUT_MS: int = 20000
    BROWSER_LOADING_TIMEOUT_MS: int = 60000
    BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS: int = 60 * 1000  # 1 minute
    OPTION_LOADING_TIMEOUT_MS: int = 600000
    MAX_STEPS_PER_RUN: int = 10
    MAX_STEPS_PER_TASK_V2: int = 25
    MAX_ITERATIONS_PER_TASK_V2: int = 10
    MAX_NUM_SCREENSHOTS: int = 10
    # Ratio should be between 0 and 1.
    # If the task has been running for more steps than this ratio of the max steps per run, then we'll log a warning.
    LONG_RUNNING_TASK_WARNING_RATIO: float = 0.95
    MAX_RETRIES_PER_STEP: int = 5
    DEBUG_MODE: bool = False
    DATABASE_STRING: str = (
        "postgresql+asyncpg://skyvern@localhost/skyvern"
        if platform.system() == "Windows"
        else "postgresql+psycopg://skyvern@localhost/skyvern"
    )
    DATABASE_REPLICA_STRING: str | None = None
    DATABASE_STATEMENT_TIMEOUT_MS: int = 60000
    DISABLE_CONNECTION_POOL: bool = False
    PROMPT_ACTION_HISTORY_WINDOW: int = 1
    TASK_RESPONSE_ACTION_SCREENSHOT_COUNT: int = 3

    ENV: str = "local"
    EXECUTE_ALL_STEPS: bool = True
    JSON_LOGGING: bool = False
    LOG_RAW_API_REQUESTS: bool = True
    LOG_LEVEL: str = "INFO"
    PORT: int = 8000
    ALLOWED_ORIGINS: list[str] = ["*"]
    BLOCKED_HOSTS: list[str] = ["localhost"]
    ALLOWED_HOSTS: list[str] = []

    # Format: "http://<username>:<password>@host:port, http://<username>:<password>@host:port, ...."
    HOSTED_PROXY_POOL: str = ""
    ENABLE_PROXY: bool = False

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

    # Notification registry settings ("local" or "redis")
    NOTIFICATION_REGISTRY_TYPE: str = "local"
    NOTIFICATION_REDIS_URL: str | None = None  # Deprecated: falls back to REDIS_URL

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

    SKYVERN_TELEMETRY: bool = True
    ANALYTICS_ID: str = "anonymous"

    # email settings
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 25
    SMTP_USERNAME: str = "username"
    SMTP_PASSWORD: str = "password"

    # browser settings
    BROWSER_LOCALE: str | None = None  # "en-US"
    BROWSER_TIMEZONE: str = "America/New_York"
    BROWSER_WIDTH: int = 1920
    BROWSER_HEIGHT: int = 1080
    BROWSER_POLICY_FILE: str = "/etc/chromium/policies/managed/policies.json"
    BROWSER_LOGS_ENABLED: bool = True
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

    # task generation settings
    PROMPT_CACHE_WINDOW_HOURS: int = 24

    #####################
    # LLM Configuration #
    #####################
    # ACTIVE LLM PROVIDER
    LLM_KEY: str = "OPENAI_GPT4O"  # This is the model name
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
    # COMMON
    LLM_CONFIG_TIMEOUT: int = 300
    LLM_CONFIG_MAX_TOKENS: int = 4096
    LLM_CONFIG_TEMPERATURE: float = 0
    LLM_CONFIG_SUPPORT_VISION: bool = True  # Whether the model supports vision
    LLM_CONFIG_ADD_ASSISTANT_PREFIX: bool = False  # Whether to add assistant prefix
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
    # ANTHROPIC
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_CUA_LLM_KEY: str = "ANTHROPIC_CLAUDE3.7_SONNET"

    # VOLCENGINE (Doubao)
    ENABLE_VOLCENGINE: bool = False
    VOLCENGINE_API_KEY: str | None = None
    VOLCENGINE_API_BASE: str = "https://ark.cn-beijing.volces.com/api/v3"
    VOLCENGINE_CUA_LLM_KEY: str = "VOLCENGINE_DOUBAO_1_5_THINKING_VISION_PRO"

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

    # Where credentials are stored: bitwarden or azure_vault
    CREDENTIAL_VAULT_TYPE: str = "bitwarden"

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

    # Debug Session Settings
    DEBUG_SESSION_TIMEOUT_MINUTES: int = 20
    """
    The timeout for a persistent browser session backing a debug session,
    in minutes.
    """

    DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES: int = 5
    """
    If there are `DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES` or more minutes left
    in the persistent browser session (`started_at` + `timeout_minutes`), then
    the `timeout_minutes` of the persistent browser session can be extended.
    Otherwise we'll consider the persistent browser session to be expired.
    """

    ENCRYPTOR_AES_SECRET_KEY: str = "fillmein"
    ENCRYPTOR_AES_SALT: str | None = None
    ENCRYPTOR_AES_IV: str | None = None

    # Cleanup Cron Settings
    ENABLE_CLEANUP_CRON: bool = False
    """Enable periodic cleanup of temporary data (temp files and stale processes)."""
    CLEANUP_CRON_INTERVAL_MINUTES: int = 10
    """Interval in minutes for the cleanup cron job."""
    CLEANUP_STALE_TASK_THRESHOLD_HOURS: int = 24
    """Tasks/workflows not updated for this many hours are considered stale (stuck)."""

    # OpenTelemetry Settings
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "skyvern"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"
    OTEL_METRICS_ENABLED: bool = True
    OTEL_LOGS_ENABLED: bool = True
    OTEL_EXPORTER_INSECURE: bool = True

    # script generation settings
    WORKFLOW_START_BLOCK_LABEL: str = "__start_block__"

    def get_model_name_to_llm_key(self) -> dict[str, dict[str, str]]:
        """
        Keys are model names available to blocks in the frontend. These map to key names
        in LLMConfigRegistry._configs.
        """
        mapping: dict[str, dict[str, str]] = {
            "gemini-2.5-pro-preview-05-06": {"llm_key": "VERTEX_GEMINI_2.5_PRO", "label": "Gemini 2.5 Pro"},
            "gemini-2.5-flash": {
                "llm_key": "VERTEX_GEMINI_2.5_FLASH",
                "label": "Gemini 2.5 Flash",
            },
            "gemini-3-pro-preview": {"llm_key": "VERTEX_GEMINI_3.0_PRO", "label": "Gemini 3 Pro"},
            "gemini-3.0-flash": {"llm_key": "VERTEX_GEMINI_3.0_FLASH", "label": "Gemini 3 Flash"},
            "gemini-2.5-flash-lite": {
                "llm_key": "VERTEX_GEMINI_2.5_FLASH_LITE",
                "label": "Gemini 2.5 Flash Lite",
            },
        }

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

        return mapping

    # Ordered mapping from ENABLE_* flag names to their default LLM key.
    # Used by model_post_init to auto-resolve LLM_KEY when the default
    # "OPENAI_GPT4O" is active but the corresponding provider is disabled.
    _PROVIDER_DEFAULT_LLM_KEYS: list[tuple[str, str]] = [
        ("ENABLE_OPENAI", "OPENAI_GPT4O"),
        ("ENABLE_ANTHROPIC", "ANTHROPIC_CLAUDE3"),
        ("ENABLE_GEMINI", "GEMINI_FLASH_2_0"),
        ("ENABLE_AZURE", "AZURE_OPENAI"),
        ("ENABLE_BEDROCK", "BEDROCK_ANTHROPIC_CLAUDE3_OPUS"),
        ("ENABLE_VERTEX_AI", "VERTEX_GEMINI_2.5_PRO"),
        ("ENABLE_OLLAMA", "OLLAMA"),
        ("ENABLE_OPENROUTER", "OPENROUTER"),
        ("ENABLE_GROQ", "GROQ"),
        ("ENABLE_VOLCENGINE", "VOLCENGINE_DOUBAO_SEED_1_6"),
        ("ENABLE_NOVITA", "NOVITA_DEEPSEEK_R1"),
        ("ENABLE_MOONSHOT", "MOONSHOT_KIMI_K2"),
    ]

    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        super().model_post_init(__context)

        self._resolve_llm_key_default()

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

    def _resolve_llm_key_default(self) -> None:
        """Auto-resolve LLM_KEY when it is still the hardcoded default but its provider is disabled.

        When a user configures a non-OpenAI provider (e.g. ENABLE_GEMINI=true) without
        explicitly setting LLM_KEY, the default "OPENAI_GPT4O" would cause runtime failures
        because that key is only registered when ENABLE_OPENAI=true. This method detects the
        mismatch and selects the first enabled provider's default key instead.
        """
        if self.LLM_KEY != "OPENAI_GPT4O" or self.ENABLE_OPENAI:
            return

        for flag_name, default_key in self._PROVIDER_DEFAULT_LLM_KEYS:
            if getattr(self, flag_name, False):
                LOG.info(
                    "LLM_KEY was not explicitly set and ENABLE_OPENAI is disabled. "
                    "Auto-selecting LLM_KEY='%s' based on %s=true.",
                    default_key,
                    flag_name,
                )
                object.__setattr__(self, "LLM_KEY", default_key)
                return

    def is_cloud_environment(self) -> bool:
        """
        :return: True if env is not local, else False
        """
        return self.ENV != "local"

    def execute_all_steps(self) -> bool:
        """
        This provides the functionality to execute steps one by one through the Streamlit UI.
        ***Value is always True if ENV is not local.***

        :return: True if env is not local, else the value of EXECUTE_ALL_STEPS
        """
        if self.is_cloud_environment():
            return True
        else:
            return self.EXECUTE_ALL_STEPS


settings = Settings()
