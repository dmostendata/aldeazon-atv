# app/services/logger_service.py
import traceback
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.telemetry import ConsultaModel, ErrorLogModel

class TelemetryLogger:
    """
    We use static/async methods to record audit trails without blocking the main thread.
    """
    
    @staticmethod
    async def log_consulta(
        db: AsyncSession,
        vendedor_hash: str,
        consulta_sanitizada: str,
        intencion: str,
        confianza: float,
        lat_total_ms: int,
        version_corpus: int,
        escalada_hitl: bool = False,
        respuesta: Optional[str] = None,
        modelo: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_hit: bool = False,
        registros_usados: Optional[list] = None
    ) -> ConsultaModel:
        
        # Simple cost estimation: $0.15 per 1M input, $0.60 per 1M output (gpt-4o-mini rates)
        costo = (tokens_in * 0.00000015) + (tokens_out * 0.00000060)
        
        log_entry = ConsultaModel(
            vendedor_hash=vendedor_hash,
            consulta_sanitizada=consulta_sanitizada,
            intencion=intencion,
            confianza=confianza,
            escalada_hitl=escalada_hitl,
            respuesta=respuesta,
            modelo=modelo,
            tokens_entrada=tokens_in,
            tokens_salida=tokens_out,
            costo_estimado_usd=costo,
            lat_total_ms=lat_total_ms,
            cache_hit=cache_hit,
            registros_usados=registros_usados,
            version_corpus=version_corpus
        )
        
        # PHP Parallel: Equivalent to $logEntry->save() in Eloquent
        db.add(log_entry)
        await db.commit()
        await db.refresh(log_entry)
        return log_entry

    @staticmethod
    async def log_error(
        db: AsyncSession,
        componente: str,
        excepcion: Exception,
        consulta_id: Optional[str] = None
    ) -> ErrorLogModel:
        """
        Catches unhandled exceptions and writes them to PostgreSQL for Apache Superset alerting.
        """
        error_entry = ErrorLogModel(
            consulta_id=consulta_id,
            componente=componente,
            excepcion_tipo=type(excepcion).__name__,
            mensaje=str(excepcion),
            # Extract formatted Python traceback (similar to PHP's $e->getTraceAsString())
            stack_trace="".join(traceback.format_tb(excepcion.__traceback__))
        )
        
        db.add(error_entry)
        await db.commit()
        return error_entry