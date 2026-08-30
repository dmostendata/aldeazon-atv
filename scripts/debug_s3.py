# scripts/debug_s3.py
import boto3
from botocore.config import Config
from app.core.config import settings

def debug_s3_namespace():
    """
    Equivalente a 'php artisan tinker' para depurar la estructura de S3.
    Imprime los buckets disponibles y el contenido exacto del bucket configurado.
    """
    print(f"[*] Conectando a SeaweedFS en {settings.s3_endpoint_url}...")
    
    # 1. Instanciar cliente síncrono forzando Path-Style
    s3 = boto3.client('s3',
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(s3={'addressing_style': 'path'}, signature_version='s3v4')
    )
    
    # 2. Listar todos los Buckets a los que el usuario tiene acceso
    print("\n[*] BUCKETS DISPONIBLES EN LA RAÍZ:")
    try:
        buckets = s3.list_buckets()
        for b in buckets.get('Buckets', []):
            print(f"  -> {b['Name']}")
    except Exception as e:
        print(f"  [!] Error listando buckets: {e}")

    # 3. Intentar listar el contenido del bucket declarado en el .env
    target_bucket = settings.s3_bucket_corpus
    print(f"\n[*] INSPECCIONANDO EL BUCKET CONFIGURADO (.env): '{target_bucket}'")
    try:
        response = s3.list_objects_v2(Bucket=target_bucket)
        contents = response.get('Contents', [])
        
        if contents:
            print(f"  [+] Se encontraron {len(contents)} objetos. Primeros 10:")
            for obj in contents[:10]:
                print(f"    - {obj['Key']} ({obj['Size']} bytes)")
        else:
            print("  [-] El bucket existe, pero está VACÍO. (Revisa si los archivos están en otro bucket o si hay un error de mayúsculas/minúsculas).")
            
    except Exception as e:
        print(f"  [!] Error leyendo el bucket '{target_bucket}': {e}")

if __name__ == "__main__":
    debug_s3_namespace()