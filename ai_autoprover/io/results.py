"""Push the pipeline's certora/ outputs to the configured results destination."""

from pathlib import Path

import smart_open


def upload_results(local_dir: Path, results_dir: str) -> None:
    """Recursively copy every file under `local_dir` to `results_dir`.

    `results_dir` may be a local path or an s3:// (or http(s)://) URL —
    smart_open dispatches on the scheme. For local destinations we make
    intermediate directories on demand.

    No-op if `local_dir` doesn't exist (e.g. the pipeline crashed before
    autosetup produced anything).
    """
    if not local_dir.exists():
        return

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
            # Stream in chunks for the same reason as in sources.py.
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
