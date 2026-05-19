"""S3-aware autoprove entry point.

Pulls inputs (contract sources, design doc) from local paths or S3 URLs via
AUTOPROVER_* env vars, runs the standard console-autoprove pipeline, and
pushes outputs back to a configurable destination (local or S3).

Designed for cloud deployments where bind-mounting a host filesystem isn't
practical. For local dev, prefer the positional-arg `console-autoprove`
entry point.
"""

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# The `[s3]` extra (smart_open + boto3 + pydantic-settings) is installed by
# the cloud Dockerfile but NOT by the local one — so we defer those imports
# into main(). That lets `pip install -e .` register the `s3-autoprove`
# entry point cleanly in either image; running it without the extra
# installed produces an explicit ImportError instead of a confusing
# module-load crash.


def _rag_conn_from_env() -> str:
    """Mirror the entrypoint's RAG-DB construction so the connection string
    points at the compose-managed postgres regardless of where this runs.
    The bare `console-autoprove` flow has this injected by the shell
    entrypoint; here we build sys.argv ourselves so we do it directly."""
    host = os.environ.get("CERTORA_AI_COMPOSER_PGHOST", "localhost")
    port = os.environ.get("CERTORA_AI_COMPOSER_PGPORT", "5432")
    return f"postgresql://rag_user:rag_password@{host}:{port}/rag_db"


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


def main() -> int:
    # Deferred imports — see the module docstring above.
    from ai_autoprover.autosetup.handler import install_handler
    from ai_autoprover.io.results import upload_results
    from ai_autoprover.io.settings import AutoproverSettings
    from ai_autoprover.io.sources import prepare_sources

    settings = AutoproverSettings()
    install_handler()

    # 1. Bring sources into a local directory (no-op if pre-populated).
    prepare_sources(settings)

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
        upload_results(settings.sources_dir / "certora", settings.results_dir)

    return rc
