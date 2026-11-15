from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PORTAL_")

    app_name: str = "Portal de ofertas · Corte láser 2D"
    database_url: str = "sqlite:///./portal.db"
    max_upload_mb: int = 15
    max_thickness_mm: float = 12.0
    min_thickness_mm: float = 0.5
    # Conversor DWG→DXF opcional (LibreDWG `dwg2dxf` u ODA File Converter envuelto en script)
    dwg_converter: str = "dwg2dxf"


settings = Settings()
