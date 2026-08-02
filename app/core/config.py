# app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application Configuration Singleton.
    
    PHP/Laravel Parallel: Equivalent to config/app.php and config/services.php
    backed by Dotenv. Pydantic enforces strict runtime typing on boot.
    """
    database_url: str
    
    # Microsoft Graph API Credentials
    ms_tenant_id: str
    ms_client_id: str
    ms_client_secret: str
    ms_site_id: str
    ms_drive_id: str

    # Meta / WhatsApp Credentials
    meta_verify_token: str = "aldeazon_atv_secure_token_2026"
    meta_app_secret: str = "your_meta_app_secret"
    
    # Hugging Face Authentication (Prevents Hub rate-limit warnings)
    hf_token: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )


settings = Settings()

# Pythonic Pattern: Automatically export the token to os.environ so low-level
# C/C++ or ONNX libraries (huggingface_hub) detect it implicitly.
if settings.hf_token:
    os.environ["HF_TOKEN"] = settings.hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = settings.hf_token