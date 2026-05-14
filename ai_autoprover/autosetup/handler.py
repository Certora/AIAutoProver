from pathlib import Path
import tempfile
import sys
import json
import asyncio
from composer.spec.source.autosetup import (
    _setup_impl, SetupSuccess, SetupFailure, SetupResult,
    SetupLifecycleCallbacks
)

async def _handle(
    callbacks: SetupLifecycleCallbacks,
    project_root: Path,
    relative_path: str,
    main_contract: str,
    *extra_files
) -> SetupResult:
    certora_dir = project_root / "certora"
    contract_name = main_contract
    with tempfile.NamedTemporaryFile("r") as f:
        main_contract_path = f"{relative_path}:{contract_name}"
        args = [
            sys.executable, "-m", "certora_autosetup.autosetup",
            "--composer-setup", f.name,
            "--skip-hashing-bound-detection", "1024",
            "--use-local-runner",
            "--no-strip-contracts",
            "--main-contract",
            main_contract_path,
            main_contract_path,
            *extra_files
        ]
        callbacks.log_start()
        proc = await asyncio.subprocess.create_subprocess_exec(
            *args,
            cwd=project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode().rstrip()
            if not line:
                continue
            callbacks.log_stdout(line=line)
        returncode = await proc.wait()
        callbacks.log_complete(returncode)
        if returncode != 0:
            return SetupFailure(
                error=f"AutoSetup failed",
            )

        data = json.load(f)

    summary_path = Path(data["contract_to_summary"][main_contract])
    resolved_summary_path : Path
    if summary_path.is_absolute():
        if not summary_path.is_relative_to(certora_dir):
            return SetupFailure(error="Summary not in project relative path")
        else:
            resolved_summary_path = summary_path
    else:
        if summary_path.parts[0] != "certora":
            return SetupFailure(error="Summary not in certora/ folder")
        resolved_summary_path = project_root / summary_path
        if not resolved_summary_path.exists() or not resolved_summary_path.is_relative_to(certora_dir):
            return SetupFailure(error=f"Relative path {summary_path} doesn't exist in project certora/ folder")

    udts = json.loads((project_root / ".certora_internal" / "all_user_defined_types.json").read_text())

    return SetupSuccess(
        prover_config=json.loads((project_root / data["contract_to_config"][main_contract]).read_text()),
        summaries_path=str(resolved_summary_path.relative_to(certora_dir)),
        user_types=udts
    )

def install_handler():
    _setup_impl.set(_handle)
