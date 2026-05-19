# AI Autoprover — Containerized Execution

AI Autoprover wires the [AI Composer](https://github.com/Certora/AIComposer) auto-prove pipeline up to the [Certora Autosetup](https://github.com/Certora/Autosetup) toolchain. This document covers the containerized execution path — the `console-autoprove` entry point — running against a local PostgreSQL service and the Certora cloud prover. For pipeline internals and CLI argument descriptions, see AIComposer's `AUTOPROVE.md`.

The container is **ephemeral** — drop and rebuild any time. The five PostgreSQL databases (rag, langgraph store/checkpoint, memory, audit) live in a named docker volume (`postgres_data`) and persist across container drops. Only **cloud prover mode** is supported; you must supply `CERTORAKEY`.

The image bakes in:

- The Python venv with `ai-autoprover`, `ai-composer[ml,prover]`, and `certora-autosetup` installed (versions pinned by `pyproject.toml`)
- Eclipse Temurin 21 JRE — used by `certora_cli` for local CVL syntax checking
- The entire solidity compilers [collection](https://github.com/Certora/cvt-executables-linux); `solc` linked to `/usr/local/bin/solc8.29` as the default
- The `nomic-embed-text-v1.5` sentence-transformer model
- Pre-rendered CVL/prover documentation HTML, used by `setup-db` to populate `rag_db`

## Build

The build clones private Certora repos via pip (transitively, through `pyproject.toml`'s URL pins), so it needs your SSH agent forwarded via BuildKit:

```bash
# Load an SSH key authorized for the Certora org
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519   # or whichever key

docker compose -f scripts/docker-compose.yml --profile autoprove build
```

If `docker compose build` doesn't forward SSH on your installation, fall back to a direct buildx invocation:

```bash
DOCKER_BUILDKIT=1 docker buildx build --ssh default \
    -t aiautoprover:latest \
    -f scripts/Dockerfile.autoprove .
```

## Start the database (once per host reboot)

```bash
docker compose -f scripts/docker-compose.yml up -d postgres
```

This brings up only the persistent postgres service. The `autoprove` service is profile-gated and intended for one-shot `run --rm` invocations.

## One-time DB setup

After postgres is up and the image is built, populate the databases. The `setup-db` subcommand applies the schema (via psql against the in-image `init-db.sql` shipped by `ai-composer`), then populates `rag_db` and the LangGraph knowledge base:

```bash
docker compose -f scripts/docker-compose.yml --profile autoprove \
    run --rm autoprove setup-db
```

`setup-db` is idempotent on the schema step (skips init if `rag_user` already exists); only re-run it if you rebuild the image with newer docs or want to refresh the knowledge base.

## Running autoprove

```bash
export ANTHROPIC_API_KEY=...
export CERTORAKEY=...
# HOST_WORK_DIR defaults to $PWD; everything under it shows up at /work in the container.
export HOST_WORK_DIR=/path/to/your/projects
# Run as your host user so /work outputs aren't owned by root inside the container.
export HOST_UID=$(id -u) HOST_GID=$(id -g)

docker compose -f scripts/docker-compose.yml --profile autoprove \
    run --rm autoprove \
    console-autoprove --cloud \
    /work/my-defi-protocol \
    /work/my-defi-protocol/src/Vault.sol:Vault \
    /work/my-defi-protocol/docs/vault-design.pdf
```

The entrypoint injects `--rag-db postgresql://rag_user:rag_password@postgres:5432/rag_db` automatically — supply your own `--rag-db` to override. Outputs under `<project_root>/certora/` land back on your host because `HOST_WORK_DIR` is a bind mount.

## What persists across container drops

| Lives in | Survives `--rm`? |
|---|---|
| `postgres_data` named volume (all five DBs) | yes |
| Files under `HOST_WORK_DIR` on the host (project + `certora/` outputs) | yes |
| Anything else inside the container (logs, tmp) | no |
