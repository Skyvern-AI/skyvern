from skyvern.config import Settings
from skyvern.config import settings as base_settings
from pydantic import Field  # Import Field from pydantic
from pydantic_settings import BaseSettings  # Import BaseSettings from pydantic_settings


class Settings(BaseSettings):
    # Base configuration
    ENV: str = Field(default="local")
    
    # Llama Configuration
    ENABLE_LLAMA: bool = Field(default=False, env="ENABLE_LLAMA")
    LLAMA_API_BASE: str = Field(default="http://localhost:11434", env="LLAMA_API_BASE")
    LLAMA_MODEL_NAME: str = Field(default="llama3.2-vision", env="LLAMA_MODEL_NAME")
    LLAMA_API_ROUTE: str = Field(default="/api/chat", env="LLAMA_API_ROUTE")
    
    # Disable other providers
    ENABLE_OPENAI: bool = Field(default=False, env="ENABLE_OPENAI")
    ENABLE_ANTHROPIC: bool = Field(default=False, env="ENABLE_ANTHROPIC")  
    ENABLE_AZURE: bool = Field(default=False, env="ENABLE_AZURE")
    ENABLE_AZURE_GPT4O_MINI: bool = Field(default=False, env="ENABLE_AZURE_GPT4O_MINI")
    ENABLE_BEDROCK: bool = Field(default=False, env="ENABLE_BEDROCK")

    # LLM Configuration
    LLM_KEY: str = Field(default="LLAMA3")
    LLM_CONFIG_TIMEOUT: int = Field(default=300)
    LLM_CONFIG_MAX_TOKENS: int = Field(default=16384)
    LLM_CONFIG_TEMPERATURE: float = Field(default=0)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


class SettingsManager:
    _instance = None
    
    @staticmethod 
    def get_settings():
        if SettingsManager._instance is None:
            print("\n=== Initializing Settings ===")
            from skyvern.config import Settings
            SettingsManager._instance = Settings(_env_file=".env")
            print("Settings values:", {
                "ENABLE_LLAMA": SettingsManager._instance.ENABLE_LLAMA,
                "LLM_KEY": SettingsManager._instance.LLM_KEY,
                "LLAMA_API_BASE": SettingsManager._instance.LLAMA_API_BASE,
                "LLAMA_MODEL_NAME": SettingsManager._instance.LLAMA_MODEL_NAME,
                "env_file": ".env"
            })
        return SettingsManager._instance

    @staticmethod
    def set_settings(settings: Settings) -> None:
        SettingsManager.__instance = settings
