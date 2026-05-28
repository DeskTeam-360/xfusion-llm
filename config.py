import logging
from pydantic_settings import BaseSettings, SettingsConfigDict

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("xfusion-backend")

class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # We set default strings to avoid app crashes before environment keys are set.
    # We will validate key presence when the endpoints or databases are initialized.
    API_KEY: str = "super_secret_wordpress_token"
    OPENAI_API_KEY: str = ""
    CHROMA_PERSIST_DIR: str = "./chroma_db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()

# Validate configuration on import
if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY.startswith("sk-proj-..."):
    logger.warning("WARNING: OPENAI_API_KEY is not set or has placeholder value. The application will not be able to evaluate exams or embed text until configured.")
