"""Pydantic-settings-based config for the S3-aware autoprove entry point.

All fields are populated from environment variables prefixed `AUTOPROVER_`,
e.g. `AUTOPROVER_CONTRACT_PATH`, `AUTOPROVER_DESIGN_MD_PATH`. URL-style
inputs (s3://..., http(s)://...) flow through `smart_open` later, so local
and remote paths are handled uniformly without code branching here.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AutoproverSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="autoprover_")

    contract_path: Path
    """Path (relative to sources_dir) of the Solidity contract to verify."""

    contract_name: str
    """Name of the Solidity contract to verify (e.g. `Vault`)."""

    design_md_path: str
    """Local path or URL (s3://, http(s)://) to the design document.
    Typed as str rather than Path because pathlib mangles URLs (collapses
    the `//` after the scheme)."""

    sources_dir: Path
    """Local directory where the Solidity sources live or are extracted to."""

    sources_zip_url: str | None = None
    """Optional URL (s3://, http(s)://) to a ZIP archive of the sources.
    When set, the archive is downloaded and unpacked into `sources_dir`
    before the pipeline runs."""

    results_dir: str
    """Destination for the certora/ outputs after the run. May be a local
    path or s3://... — same str-not-Path rationale as design_md_path."""
