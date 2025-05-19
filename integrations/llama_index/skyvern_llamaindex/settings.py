from dotenv import load_dotenv
from pydantic_settings import BaseSettings

from skyvern.schemas.runs import RunEngine


class Settings(BaseSettings):
    api_key: str = ""
    base_url: str = "https://api.skyvern.com"
    engine: RunEngine = RunEngine.skyvern_v2
    run_task_timeout_seconds: int = 60 * 60

    class Config:
        env_prefix = "SKYVERN_"


load_dotenv()
settings = Settings()
