from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVOICE_LAYOUT_", extra="forbid")

    provider: Literal["host", "local", "auto"] = "auto"
    host_manifest: Path | None = None
    page_margin_mm: float = Field(default=13.5, ge=12, le=15)
    item_gap_mm: float = Field(default=8, ge=6, le=10)
    render_dpi: int = Field(default=300, ge=200, le=600)
    work_dir: Path = Path(".invoice-layout-work")

    @field_validator("host_manifest")
    @classmethod
    def host_manifest_is_file_path(cls, value: Path | None) -> Path | None:
        """Keep the manifest path declarative; loading verifies its content later."""
        if value is not None and value.is_dir():
            raise ValueError("host_manifest must be a file path")
        return value

    def resolved_provider(self) -> Literal["host", "local"]:
        if self.provider != "auto":
            return self.provider
        return "host" if self.host_manifest is not None else "local"
