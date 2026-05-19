"""Materialize Solidity sources into a local directory before the run."""

import tempfile
import zipfile

import smart_open

from ai_autoprover.io.settings import AutoproverSettings


def prepare_sources(settings: AutoproverSettings) -> None:
    """Populate `settings.sources_dir` from `settings.sources_zip_url`.

    No-op when `sources_zip_url` is unset — we assume the directory is
    already populated (e.g. via a bind mount). When the URL is set,
    smart_open streams the archive from any supported scheme (s3://,
    http(s)://, local file) into a tempfile, then extracts to sources_dir.
    """
    if not settings.sources_zip_url:
        return

    settings.sources_dir.mkdir(parents=True, exist_ok=True)

    with smart_open.open(settings.sources_zip_url, "rb") as src, \
         tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        # Stream in chunks to avoid loading a multi-gig archive into RAM.
        while chunk := src.read(1024 * 1024):
            tmp.write(chunk)
        tmp.flush()
        with zipfile.ZipFile(tmp.name, "r") as zf:
            zf.extractall(settings.sources_dir)
