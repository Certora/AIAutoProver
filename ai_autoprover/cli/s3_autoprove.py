"""S3-aware autoprove entry point.

Self-contained: pulls inputs (contract sources, design doc) from local
paths or S3 URLs via `AUTOPROVER_*` env vars, runs the standard
console-autoprove pipeline, and pushes outputs back to a configurable
destination (local path or s3:// URL).

The `[s3]` extra (smart_open + boto3 + pydantic-settings) is installed by
the cloud Dockerfile but not by the local one — so every symbol that
extra provides is imported inside the function that uses it. That lets
`pip install -e .` register the `s3-autoprove` entry point cleanly in
either image; invoking it from the local image produces a clean
ImportError instead of a confusing module-load crash.
"""

import os
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse


def _rag_conn_from_env() -> str:
    """Mirror the entrypoint's RAG-DB construction so the connection string
    points at the compose-managed postgres regardless of where this runs.
    `console-autoprove` (bare) has this injected by the shell entrypoint;
    here we build sys.argv ourselves so we do it directly."""
    host = os.environ.get("CERTORA_AI_COMPOSER_PGHOST", "localhost")
    port = os.environ.get("CERTORA_AI_COMPOSER_PGPORT", "5432")
    return f"postgresql://rag_user:rag_password@{host}:{port}/rag_db"


def _load_settings():
    """Read `AUTOPROVER_*` env vars into a typed settings object.

    `AutoproverSettings` is defined inside this factory rather than at
    module level so `pydantic_settings` (part of the [s3] extra) is
    imported lazily — see the module docstring.
    """
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class AutoproverSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="autoprover_")

        contract_path: Path
        """Path (relative to sources_dir) of the Solidity contract to verify."""

        contract_name: str
        """Name of the Solidity contract to verify (e.g. `Vault`)."""

        design_md_path: str
        """Local path or URL (s3://, http(s)://) to the design document.
        Typed as str rather than Path because pathlib mangles URLs
        (collapses the `//` after the scheme)."""

        sources_dir: Path
        """Local directory where the Solidity sources live or get extracted to."""

        sources_zip_url: str | None = None
        """Optional URL (s3://, http(s)://) to a ZIP of the sources. When
        set, the archive is fetched and unpacked into sources_dir before
        the pipeline runs."""

        results_dir: str
        """Destination for the certora/ outputs after the run. May be a
        local path or s3:// — same str-not-Path rationale as design_md_path."""

    return AutoproverSettings()


def _prepare_sources(sources_dir: Path, sources_zip_url: str | None) -> None:
    """Populate sources_dir from sources_zip_url, no-op if no URL set."""
    if not sources_zip_url:
        return
    import smart_open

    sources_dir.mkdir(parents=True, exist_ok=True)
    with smart_open.open(sources_zip_url, "rb") as src, \
         tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        # Stream chunks rather than slurping the whole archive into RAM.
        while chunk := src.read(1024 * 1024):
            tmp.write(chunk)
        tmp.flush()
        with zipfile.ZipFile(tmp.name, "r") as zf:
            zf.extractall(sources_dir)


def _stage_design_doc(design_md_path: str, work_dir: Path) -> Path:
    """Download the design doc to a local file, preserving its extension.

    Composer checks `Path.suffix == '.pdf'` to decide between
    base64-encoding and text-reading, so we must keep the suffix from the
    source URL (e.g. `design.pdf` -> `.pdf`, `design.md` -> `.md`).
    """
    import smart_open

    suffix = Path(urlparse(design_md_path).path).suffix or ".md"
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / f"_autoprover_design{suffix}"
    with smart_open.open(design_md_path, "rb") as src:
        dest.write_bytes(src.read())
    return dest


def _upload_results(local_dir: Path, results_dir: str) -> None:
    """Recursively copy every file under local_dir to results_dir.

    results_dir may be a local path or an s3:// (or http(s)://) URL —
    smart_open dispatches on the scheme. For local destinations we make
    intermediate directories on demand. No-op if local_dir doesn't exist.
    """
    if not local_dir.exists():
        return
    import smart_open

    is_remote = results_dir.startswith("s3://") or results_dir.startswith("http")
    base = results_dir.rstrip("/")

    for file_path in local_dir.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(local_dir).as_posix()
        dest = f"{base}/{rel}"
        if not is_remote:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "rb") as src, smart_open.open(dest, "wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)


def main() -> int:
    from ai_autoprover.autosetup.handler import install_handler

    settings = _load_settings()
    install_handler()

    # 1. Bring sources into a local directory (no-op if pre-populated).
    _prepare_sources(settings.sources_dir, settings.sources_zip_url)

    # 2. Stage the design doc as a local file. Put it in a sibling tempdir
    #    rather than under sources_dir so it doesn't show up in `find` /
    #    file enumerations the LLM does on the project.
    staging = Path(tempfile.mkdtemp(prefix="autoprover-"))
    design_doc_local = _stage_design_doc(settings.design_md_path, staging)

    # 3. Hand off to composer's console-autoprove by populating sys.argv.
    contract_full = settings.sources_dir / settings.contract_path
    from composer.cli.console_autoprove import main as console_main

    sys.argv = [
        "console-autoprove",
        "--cloud",
        "--rag-db", _rag_conn_from_env(),
        str(settings.sources_dir),
        f"{contract_full}:{settings.contract_name}",
        str(design_doc_local),
    ]

    rc = 1
    try:
        rc = console_main()
    finally:
        # 4. Push results. We do this in `finally` so partial outputs from
        #    a crashed run are still recoverable.
        _upload_results(settings.sources_dir / "certora", settings.results_dir)

    return rc
