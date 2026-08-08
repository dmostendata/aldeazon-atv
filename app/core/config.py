# app/core/config.py
import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application Configuration Singleton.
    """
    # --- PostgreSQL 18 Traceability Database (VPS3) ---
    database_url: str = "postgresql+psycopg://aldeazon_user:secret@localhost:5434/atv_qa"
    
    # --- Internal VPC Security (App AldeaZON -> ATV Endpoint) ---
    api_key: str = "aldeazon_internal_app_secret_2026"
    
    # --- On-Premise Object Storage (SeaweedFS S3 - VPS3) ---
    # Replaces deprecated Microsoft Graph / SharePoint credentials
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str = "seaweedfs_admin"
    s3_secret_key: str = "seaweedfs_secret"
    s3_bucket_corpus: str = "atv-corpus-vigente"
    s3_region: str = "us-east-1"  # Static fallback for S3 SDK compatibility

    # --- Vector Database (Qdrant 1.9.1 - VPS4) ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: Optional[str] = None
    qdrant_collection: str = "atv_corpus"
    
    # --- Semantic Cache & Message Broker (Redis 7 - VPS4) ---
    redis_url: str = "redis://localhost:6380/3"  # Logical DB 3 reserved for ATV semantic cache
    cache_similarity_threshold: float = 0.95
    cache_ttl_seconds: int = 604800  # 7 days

    # --- External LLM Providers & Fallback ---
    openai_api_key: str = "sk-proj-placeholder"
    gemini_api_key: Optional[str] = None
    llm_primary_model: str = "gpt-4o-mini"
    llm_fallback_model: str = "gemini-1.5-flash"
    llm_timeout_seconds: int = 6

    # --- Governance & SLA Budgets ---
    sla_latency_threshold_ms: int = 10000
    confidence_threshold_recommendation: float = 0.70
    confidence_threshold_informational: float = 0.55
    confidence_threshold_abstention: float = 0.35

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