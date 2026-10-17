# app/core/config.py
import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """
    Application Configuration Singleton.
    """
    # --- PostgreSQL 18 Traceability Database (VPS3) ---
    database_url: str
    
    # --- Internal VPC Security (App AldeaZON -> ATV Endpoint) ---
    api_key: str
    
    # --- On-Premise Object Storage (SeaweedFS S3 - VPS3) ---
    # Replaces deprecated Microsoft Graph / SharePoint credentials
    s3_endpoint_url: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket_corpus: str
    s3_region: str

    # --- Vector Database (Qdrant 1.9.1 - VPS4) ---
    qdrant_url: str
    qdrant_api_key: Optional[str] = None
    qdrant_collection: str
    
    # --- Semantic Cache & Message Broker (Redis 7 - VPS4) ---
    redis_url: str
    cache_similarity_threshold: float = 0.95
    cache_ttl_seconds: int = 604800  # 7 days

    # --- External LLM Providers & Fallback ---
    openai_api_key: str
    gemini_api_key: Optional[str] = None
    llm_primary_model: str = "gpt-4o-mini"
    llm_fallback_model: str = "gemini-1.5-flash"
    llm_timeout_seconds: int = 6

    # --- Governance & SLA Budgets ---
    sla_latency_threshold_ms: int = 10000
    confidence_threshold_recommendation: float = 0.70
    confidence_threshold_informational: float = 0.55
    confidence_threshold_abstention: float = 0.35

    # --- Perfil del usuario (Semillero) ---
    # Salt del hash_determinista de Semillero (HMAC-SHA256). Sin él, la API no
    # puede reconocer a socios/clientes por cédula y perfila solo con preguntas.
    semillero_hash_salt: Optional[str] = None
    # Base `semillero` (stg_cafe, PostgreSQL de Semillero), solo lectura: la usa
    # scripts/construir_perfiles.py. Sin usuario o contraseña, la URL queda en None.
    semillero_pg_host: str = "10.0.0.14"
    semillero_pg_port: int = 5432
    semillero_pg_db: str = "semillero"
    semillero_pg_user: Optional[str] = None
    semillero_pg_password: Optional[str] = None

    @property
    def semillero_database_url(self) -> Optional[str]:
        if not (self.semillero_pg_user and self.semillero_pg_password):
            return None
        return URL.create(
            "postgresql+psycopg",
            username=self.semillero_pg_user,
            password=self.semillero_pg_password,  # URL.create escapa @, :, / y demás
            host=self.semillero_pg_host,
            port=self.semillero_pg_port,
            database=self.semillero_pg_db,
        ).render_as_string(hide_password=False)

    # --- Hugging Face Runtime Optimization ---
    hf_token: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

# Automatically export HF token to OS environment for C++/Rust tokenizers
if settings.hf_token:
    os.environ["HF_TOKEN"] = settings.hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = settings.hf_token

os.environ.setdefault("FASTEMBED_CACHE_PATH", "/var/www/aldeazon-atv/.cache/fastembed")