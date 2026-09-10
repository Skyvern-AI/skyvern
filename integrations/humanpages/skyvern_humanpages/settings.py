from dotenv import load_dotenv
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_key: str = ""
    base_url: str = "https://humanpages.ai"
    default_price_usdc: float = 5.0
    default_deadline_hours: int = 4
    poll_interval_seconds: int = 30
    poll_timeout_seconds: int = 14400  # 4 hours

    class Config:
        env_prefix = "HUMANPAGES_"


load_dotenv()
settings = Settings()
