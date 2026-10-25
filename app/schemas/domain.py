# app/schemas/domain.py
import re
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field, ConfigDict, field_validator


class RegistroAtomico(BaseModel):
    """
    Pydantic v2 DTO representing an atomic agronomic knowledge chunk.
    """
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    registro_id: str = Field(..., description="UUID v4 string identifying the atomic chunk")
    documento_fuente: str = Field(..., description="Source PDF filename in SeaweedFS S3")
    version_corpus: int = Field(..., ge=1, description="Golden corpus version integer")
    
    producto: str = Field(..., min_length=2, description="Commercial brand name in technical sheet")
    principio_activo: Optional[str] = Field(None, description="Active chemical molecule (e.g., 'Imidacloprid')")
    cultivo: str = Field(..., description="Target crop or '(varios/todos)'")
    
    # WHY OPTIONAL:
    # 4 of 9 reference sheets (fertilizers/biostimulants) target crop phenological stages
    # without a specific disease or pest. Forcing non-null would break ingestion.
    objetivo: Optional[str] = Field(None, description="Pest, disease, or growth stage")
    
    dosis: Optional[str] = Field(None, description="Literal dosage text (e.g., '1,42 - 2,14 l/ha')")
    condiciones: Optional[str] = Field(None, description="Application constraints and intervals")
    
    metodo_extraccion: str = Field(
        ..., 
        pattern="^(tabla|columnas|prosa|vision)$",
        description="Parsing strategy used by offline Airflow DAG"
    )
    estado_validacion: str = Field(
        ..., 
        pattern="^(extraido|en_revision|aprobado|rechazado)$",
        description="Expert human review state (Sergio Espinosa)"
    )
    idioma: str = Field("es", pattern="^(es|en)$", description="ISO 639-1 language code")

    @field_validator("dosis")
    @classmethod
    def validate_dosis_area_unit(cls, v: Optional[str]) -> Optional[str]:
        """
        Safety Gate: Ensures dosage strings contain explicit area units (ha/mz).
        Prevents false-positive pH or concentration values from passing ingestion.
        """
        if v:
            # Use regex to ignore whitespace variations between the slash and the unit
            patron_area = re.compile(r'/(?:\s*)?(ha|mz)\b|por\s+(ha|mz)\b', re.IGNORECASE)
            if not patron_area.search(v):
                raise ValueError(f"Invalid dosage format missing area unit: {v}")
        return v

    def texto_para_embedding(self) -> str:
        """
        Serializes DTO into natural language prose for bge-m3 dense vector indexing.
        
        WHY LIST JOIN:
        In Python, string immutability makes `+=` concatenation O(N^2) in loops.
        List appending and `str.join()` operate in O(N) linear time.
        """
        parts = [f"Producto: {self.producto}."]
        if self.principio_activo:
            parts.append(f"Principio activo: {self.principio_activo}.")
        parts.append(f"Cultivo: {self.cultivo}.")
        if self.objetivo:
            parts.append(f"Objetivo: {self.objetivo}.")
        if self.dosis:
            parts.append(f"Dosis: {self.dosis}.")
        if self.condiciones:
            parts.append(f"Condiciones: {self.condiciones}.")
        return " ".join(parts)


class ProductoComercial(BaseModel):
    """
    DTO representing a resolved commercial SKU from `stc2.agroquimico`
    joined with real-time branch inventory (`stc2.az_bodega_inventario`).
    """
    model_config = ConfigDict(frozen=True)

    producto_id: int = Field(..., description="stc2.agroquimico.id primary key")
    nombre_comercial: str = Field(..., description="stc2.agroquimico.nombre")
    ingrediente_activo: str = Field(..., description="Matched principle active molecule")
    bodega_id: int = Field(..., description="stc2.az_catalogo_bodegas.id")
    cantidad_disponible: int = Field(..., ge=1, description="Available branch stock > 0")
    precio_publico: float = Field(..., ge=0.0, description="Standard public selling price (USD)")
    precio_asociado: float = Field(..., ge=0.0, description="Discounted price for cooperative members")


class CarritoItem(BaseModel):
    """
    Actionable cart payload injected into App AldeaZON serving response.
    Subordinated to Technical Sales Governance (`RSK-NEW-04`).
    """
    sku_id: int
    nombre: str
    cantidad_sugerida: int = Field(1, ge=1)
    precio_unitario_usd: float
    bodega_origen_id: int
    requiere_validacion_tecnica: bool = Field(
        False, 
        description="True if query confidence was marginal but above abstention"
    )


class CultivoContexto(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=80)
    area: Optional[float] = Field(None, ge=0, le=100_000)
    unidad: Optional[str] = Field(None, pattern="^(mz|ha)$")


class ContextoInicial(BaseModel):
    """
    Perfil ya estructurado (p. ej. un formulario inicial). Se usa en lugar del LLM
    que extrae el perfil del texto: ese turno no gasta tokens de extracción.
    `consulta` del request sigue siendo la pregunta o el problema.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tipo_usuario: Optional[str] = Field(None, pattern="^(socio|cliente|productor|comerciante|tecnico|otro)$")
    # Solo para identificar al socio: se hashean enseguida, nunca se guardan ni van al LLM.
    cedula: Optional[str] = Field(None, max_length=20)
    telefono: Optional[str] = Field(None, max_length=20)
    cultivos: List[CultivoContexto] = Field(default_factory=list, max_length=10)
    municipio: Optional[str] = Field(None, max_length=80)
    comunidad: Optional[str] = Field(None, max_length=80)
    departamento: Optional[str] = Field(None, max_length=80)
    etapa_cultivo: Optional[str] = Field(None, max_length=80)
    topografia: Optional[str] = Field(None, max_length=40)
    tipo_suelo: Optional[str] = Field(None, max_length=80)
    altura_msnm: Optional[float] = Field(None, ge=0, le=5000)
    tipo_negocio: Optional[str] = Field(None, max_length=80)


class ChatResponse(BaseModel):
    """
    REST API contract for `POST /api/v1/consultar` consumed by App AldeaZON.
    """
    respuesta: str = Field(..., description="LLM generated advisory or standard HITL/Abstention text")
    fuente: Optional[str] = Field(None, description="Primary technical sheet citation and version")
    escalada_hitl: bool = Field(..., description="True if query routed to Sergio Espinosa / Asistencia Técnica")
    latencia_ms: int = Field(..., ge=0, description="Total request execution time in milliseconds")
    carrito: List[CarritoItem] = Field(
        default_factory=list, 
        description="Commercial SKUs suggested. STRICTLY EMPTY if escalada_hitl is True or query abstained."
    )
    sesion_id: Optional[str] = Field(None, description="Sesión de la conversación: la web la reenvía en cada mensaje")
    etapa: str = Field(
        "respuesta",
        pattern="^(perfilamiento|respuesta)$",
        description="'perfilamiento': el ATV está preguntando por el perfil; 'respuesta': respuesta técnica del RAG",
    )
    perfil: Optional[Dict[str, Any]] = Field(
        None, description="Perfil acumulado del usuario (sin datos personales)"
    )
    latencia_pasos: Optional[Dict[str, Any]] = Field(
        None, description="Latencia por paso (app/services/traza.py): total_ms, pasos [{paso, componente, inicio_ms, ms}], por_componente"
    )
    consumo_llm: Optional[Dict[str, Any]] = Field(
        None, description="Consumo de LLM de este mensaje, todas las llamadas: llamadas, tokens_entrada, tokens_salida, costo_usd"
    )