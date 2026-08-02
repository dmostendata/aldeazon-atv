from typing import Optional
from pydantic import BaseModel, Field

class RegistroAtomico(BaseModel):
    """
    Pydantic Model for the Atomic Record.
    This validates data extracted from SharePoint PDFs before indexing into Qdrant.
    """
    
    # Field(...) enforces that a value is strictly required
    registro_id: str = Field(..., description="UUID of the record")
    documento_fuente: str = Field(..., description="Name of the PDF in SharePoint")
    version_corpus: int = Field(..., description="Assigned during Airflow indexing phase")
    
    producto: str = Field(..., description="Commercial name, e.g., 'Consento 45 SC'")

    principio_activo: Optional[str] = None
    cultivo: str = Field(..., description="Crop, e.g., 'Café' or '(varios/todos)'")
    
    # 'objetivo' generalises plague, disease, and phenological stage, explicitly permitting None to support fertilizers.
    objetivo: Optional[str] = None
    
    dosis: Optional[str] = Field(None, description="Literal text, e.g., '1,42 - 2,14 l/ha'")
    condiciones: Optional[str] = Field(None, description="Application mode, intervals, restrictions")
    
    # Traceability attributes
    metodo_extraccion: str = Field(..., description="'tabla', 'columnas', 'prosa', or 'vision'")
    estado_validacion: str = Field(..., description="'extraido', 'en_revision', 'aprobado', or 'rechazado'")
    idioma: str = Field(..., description="'es' or 'en'")

    def texto_para_embedding(self) -> str:
        """
        Serializes the DTO into a natural language sentence for the dense retrieval phase.
        In Python, appending to a list and calling "".join() is significantly more memory-efficient 
        than standard string concatenation (+=), especially in loops, due to how strings are immutably allocated.
        """
        partes = [f"Producto: {self.producto}."]
        
        if self.principio_activo:
            partes.append(f"Principio activo: {self.principio_activo}.")
            
        partes.append(f"Cultivo: {self.cultivo}.")
        
        if self.objetivo:
            partes.append(f"Objetivo: {self.objetivo}.")
            
        if self.dosis:
            partes.append(f"Dosis: {self.dosis}.")
            
        if self.condiciones:
            partes.append(f"Condiciones: {self.condiciones}.")
            
        return " ".join(partes)