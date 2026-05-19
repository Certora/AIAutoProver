# AI Autoprover — Containerized Execution

AI Autoprover wires the [AI Composer](https://github.com/Certora/AIComposer) auto-prove pipeline up to the [Certora Autosetup](https://github.com/Certora/Autosetup) toolchain. This document covers the containerized execution paths against a local PostgreSQL service and the Certora cloud prover. For pipeline internals and CLI argument descriptions, see AIComposer's `AUTOPROVE.md`.

There are two image flavors, with separate Dockerfile / compose pairs:

| Flavor | Dockerfile | Compose | Entry point | Inputs/outputs |
|---|---|---|---|---|
| Local | `scripts/Dockerfile.local` | `scripts/docker-compose.local.yml` | `console-autoprove` | Host bind mount (`$HOST_WORK_DIR -> /work`) |
| Cloud | `scripts/Dockerfile.cloud` | `scripts/docker-compose.cloud.yml` | `s3-autoprove` | `AUTOPROVER_*` env vars (local paths or `s3://` URLs) |

Both containers are **ephemeral** — drop and rebuild any time. The five PostgreSQL databases (rag, langgraph store/checkpoint, memory, audit) live in a named docker volume (`postgres_data`) and persist across container drops. Only **cloud prover mode** is supported in either flavor; you must supply `CERTORAKEY`.

Both images bake in:

- The Python venv with `ai-autoprover`, `ai-composer[ml,prover]`, and `certora-autosetup` installed (versions pinned by `pyproject.toml`). The cloud image additionally pulls the `[s3]` extra (`pydantic-settings` + `smart_open[s3]` + boto3).
- Eclipse Temurin 21 JRE — used by `certora_cli` for local CVL syntax checking
- The entire solidity compilers [collection](https://github.com/Certora/cvt-executables-linux); `solc` linked to `/usr/local/bin/solc8.29` as the default
- The `nomic-embed-text-v1.5` sentence-transformer model
- Pre-rendered CVL/prover documentation HTML, used by `setup-db` to populate `rag_db`

The two flavors differ in their runtime user model: the local image patches `/etc/passwd` at start time so it can run as the arbitrary host UID compose targets it with; the cloud image bakes a fixed `nonroot` user (UID 65532) at build time and uses `USER nonroot` directly.

## Local bind-mount flow

### Build

The build clones private Certora repos via pip (transitively, through `pyproject.toml`'s URL pins), so it needs your SSH agent forwarded via BuildKit:

```bash
# Load an SSH key authorized for the Certora org
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519   # or whichever key

docker compose -f scripts/docker-compose.local.yml --profile autoprove build
```

If `docker compose build` doesn't forward SSH on your installation, fall back to a direct buildx invocation:

```bash
DOCKER_BUILDKIT=1 docker buildx build --ssh default \
    -t aiautoprover-local:latest \
    -f scripts/Dockerfile.local .
```

### Start the database (once per host reboot)

```bash
docker compose -f scripts/docker-compose.local.yml up -d postgres
```

This brings up only the persistent postgres service. The `autoprove` service is profile-gated and intended for one-shot `run --rm` invocations.

### One-time DB setup

After postgres is up and the image is built, populate the databases. The `setup-db` subcommand applies the schema (via psql against the in-image `init-db.sql` shipped by `ai-composer`), then populates `rag_db` and the LangGraph knowledge base:

```bash
docker compose -f scripts/docker-compose.local.yml --profile autoprove \
    run --rm autoprove setup-db
```

`setup-db` is idempotent on the schema step (skips init if `rag_user` already exists); only re-run it if you rebuild the image with newer docs or want to refresh the knowledge base.

### Running autoprove

```bash
export ANTHROPIC_API_KEY=...
export CERTORAKEY=...
# HOST_WORK_DIR defaults to $PWD; everything under it shows up at /work in the container.
export HOST_WORK_DIR=/path/to/your/projects
# Run as your host user so /work outputs aren't owned by root inside the container.
export HOST_UID=$(id -u) HOST_GID=$(id -g)

docker compose -f scripts/docker-compose.local.yml --profile autoprove \
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

## Cloud / S3 flow

For cloud deployments where bind-mounting host directories isn't practical, the cloud image's `s3-autoprove` entry point reads inputs from local paths or `s3://` (and `http(s)://`) URLs via `AUTOPROVER_*` environment variables, runs the same pipeline `console-autoprove` does, and pushes the `certora/` outputs back to a configurable destination.

The `scripts/docker-compose.cloud.yml` file is primarily a convenience for exercising the cloud code path against a local postgres — real deployments would bring up the `aiautoprover-cloud` image directly in K8s/ECS/etc., pointing `CERTORA_AI_COMPOSER_PGHOST` at a managed database (e.g. RDS).

### Build

Same SSH-agent requirement as the local flavor:

```bash
docker compose -f scripts/docker-compose.cloud.yml --profile autoprove build
```

### Environment variables

| Variable | Required | Example | Notes |
|---|---|---|---|
| `AUTOPROVER_CONTRACT_PATH` | yes | `src/Vault.sol` | Relative to `AUTOPROVER_SOURCES_DIR` |
| `AUTOPROVER_CONTRACT_NAME` | yes | `Vault` | Solidity contract name |
| `AUTOPROVER_DESIGN_MD_PATH` | yes | `s3://bucket/u/w/design.md` | Local path or URL; `.pdf` suffix triggers binary handling |
| `AUTOPROVER_SOURCES_DIR` | yes | `/tmp/sources` | Local directory; must be writable by the runtime user |
| `AUTOPROVER_SOURCES_ZIP_URL` | no | `s3://bucket/u/w/sources.zip` | If set, downloaded and extracted into `AUTOPROVER_SOURCES_DIR` |
| `AUTOPROVER_RESULTS_DIR` | yes | `s3://bucket/u/w/results` | Local path or `s3://`; `certora/` outputs are pushed here after the run (even on failure) |

AWS credentials follow the standard boto3 chain: env vars (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`), an attached IAM role (EC2 instance metadata / IRSA on EKS), or `~/.aws/credentials`. The compose file passes the env-var ones through.

### Running

```bash
export ANTHROPIC_API_KEY=... CERTORAKEY=...
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-east-1

export AUTOPROVER_CONTRACT_PATH=src/Vault.sol
export AUTOPROVER_CONTRACT_NAME=Vault
export AUTOPROVER_DESIGN_MD_PATH=s3://my-bucket/runs/abc/design.md
export AUTOPROVER_SOURCES_DIR=/tmp/sources
export AUTOPROVER_SOURCES_ZIP_URL=s3://my-bucket/runs/abc/sources.zip
export AUTOPROVER_RESULTS_DIR=s3://my-bucket/runs/abc/results

docker compose -f scripts/docker-compose.cloud.yml up -d postgres
docker compose -f scripts/docker-compose.cloud.yml --profile autoprove \
    run --rm autoprove setup-db        # one-time
docker compose -f scripts/docker-compose.cloud.yml --profile autoprove \
    run --rm autoprove                  # default CMD is s3-autoprove
```

The cloud container runs as the baked `nonroot` user (UID 65532) — there's no `HOST_UID` to set, and outputs go to `AUTOPROVER_RESULTS_DIR` rather than back to the host filesystem. The wrapper resolves all inputs (downloading the sources zip and design doc as needed), invokes `console-autoprove --cloud` under the hood with the resolved local paths, and uploads everything under `<sources_dir>/certora/` to `AUTOPROVER_RESULTS_DIR` once the run completes. Partial outputs from a failed run are still uploaded — the upload runs in a `finally` block.
