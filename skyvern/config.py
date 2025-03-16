from pydantic_settings import BaseSettings, SettingsConfigDict

from skyvern import constants
from skyvern.constants import SKYVERN_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", ".env.staging", ".env.prod"), extra="ignore")

    ADDITIONAL_MODULES: list[str] = []

    BROWSER_TYPE: str = "chromium-headful"
    MAX_SCRAPING_RETRIES: int = 0
    VIDEO_PATH: str | None = "./video"
    HAR_PATH: str | None = "./har"
    LOG_PATH: str = "./log"
    TEMP_PATH: str = "./temp"
    BROWSER_ACTION_TIMEOUT_MS: int = 5000
    BROWSER_SCREENSHOT_TIMEOUT_MS: int = 20000
    BROWSER_LOADING_TIMEOUT_MS: int = 120000
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
    DATABASE_STRING: str = "postgresql+psycopg://skyvern@localhost/skyvern"
    DATABASE_STATEMENT_TIMEOUT_MS: int = 60000
    PROMPT_ACTION_HISTORY_WINDOW: int = 1
    TASK_RESPONSE_ACTION_SCREENSHOT_COUNT: int = 3

    ENV: str = "local"
    EXECUTE_ALL_STEPS: bool = True
    JSON_LOGGING: bool = False
    LOG_LEVEL: str = "INFO"
    PORT: int = 8000
    ALLOWED_ORIGINS: list[str] = ["*"]
    BLOCKED_HOSTS: list[str] = ["localhost"]
    ALLOWED_HOSTS: list[str] = []

    # Secret key for JWT. Please generate your own secret key in production
    SECRET_KEY: str = "PLACEHOLDER"
    # Algorithm used to sign the JWT
    SIGNATURE_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # one week

    # Artifact storage settings
    ARTIFACT_STORAGE_PATH: str = f"{SKYVERN_DIR}/artifacts"
    GENERATE_PRESIGNED_URLS: bool = False
    AWS_S3_BUCKET_ARTIFACTS: str = "skyvern-artifacts"
    AWS_S3_BUCKET_SCREENSHOTS: str = "skyvern-screenshots"
    AWS_S3_BUCKET_BROWSER_SESSIONS: str = "skyvern-browser-sessions"

    # Supported storage types: local, s3
    SKYVERN_STORAGE_TYPE: str = "local"

    # S3 bucket settings
    AWS_REGION: str = "us-east-1"
    AWS_S3_BUCKET_UPLOADS: str = "skyvern-uploads"
    MAX_UPLOAD_FILE_SIZE: int = 10 * 1024 * 1024  # 10 MB
    PRESIGNED_URL_EXPIRATION: int = 60 * 60 * 24  # 24 hours

    SKYVERN_TELEMETRY: bool = True
    ANALYTICS_ID: str = "anonymous"

    # browser settings
    BROWSER_LOCALE: str = "en-US"
    BROWSER_TIMEZONE: str = "America/New_York"
    BROWSER_WIDTH: int = 1920
    BROWSER_HEIGHT: int = 1080

    # Workflow constant parameters
    WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY: str = "SKYVERN_DOWNLOAD_DIRECTORY"
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
    LLM_KEY: str = "OPENAI_GPT4O"
    SECONDARY_LLM_KEY: str | None = None
    SELECT_AGENT_LLM_KEY: str | None = None
    SINGLE_CLICK_AGENT_LLM_KEY: str | None = None
    PROMPT_BLOCK_LLM_KEY: str | None = None
    # COMMON
    LLM_CONFIG_TIMEOUT: int = 300
    LLM_CONFIG_MAX_TOKENS: int = 4096
    LLM_CONFIG_TEMPERATURE: float = 0
    # LLM PROVIDER SPECIFIC
    ENABLE_OPENAI: bool = False
    ENABLE_ANTHROPIC: bool = False
    ENABLE_AZURE: bool = False
    ENABLE_AZURE_GPT4O_MINI: bool = False
    ENABLE_AZURE_O3_MINI: bool = False
    ENABLE_BEDROCK: bool = False
    ENABLE_GEMINI: bool = False
    # OPENAI
    OPENAI_API_KEY: str | None = None
    # ANTHROPIC
    ANTHROPIC_API_KEY: str | None = None
    # AZURE
    AZURE_DEPLOYMENT: str | None = None
    AZURE_API_KEY: str | None = None
    AZURE_API_BASE: str | None = None
    AZURE_API_VERSION: str | None = None

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

    # GEMINI
    GEMINI_API_KEY: str | None = None

    # NOVITA AI
    ENABLE_NOVITA: bool = False
    NOVITA_API_KEY: str | None = None
    NOVITA_API_VERSION: str = "v3"

    # TOTP Settings
    TOTP_LIFESPAN_MINUTES: int = 10
    VERIFICATION_CODE_INITIAL_WAIT_TIME_SECS: int = 40
    VERIFICATION_CODE_POLLING_TIMEOUT_MINS: int = 15

    # Bitwarden Settings
    BITWARDEN_CLIENT_ID: str | None = None
    BITWARDEN_CLIENT_SECRET: str | None = None
    BITWARDEN_MASTER_PASSWORD: str | None = None

    # Skyvern Auth Bitwarden Settings
    SKYVERN_AUTH_BITWARDEN_CLIENT_ID: str | None = None
    SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET: str | None = None
    SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD: str | None = None
    SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID: str | None = None

    BITWARDEN_SERVER: str = "http://localhost"
    BITWARDEN_SERVER_PORT: int = 8002

    SVG_MAX_LENGTH: int = 100000

    ENABLE_LOG_ARTIFACTS: bool = False
    ENABLE_CODE_BLOCK: bool = False

    # SkyvernClient Settings
    SKYVERN_BASE_URL: str = "https://api.skyvern.com"
    SKYVERN_API_KEY: str = "PLACEHOLDER"

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
