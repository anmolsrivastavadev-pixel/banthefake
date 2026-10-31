# syntax=docker/dockerfile:1
# BanTheFake: FastAPI app + CPU-only AI-image detector, runs offline at runtime.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf

WORKDIR /app

# 1) Locked dependencies, including the `ml` extra, EXCEPT torch and its CUDA stack.
#    uv.lock pins torch from PyPI, whose Linux wheel pulls ~4 GB of nvidia/cuda/triton packages.
#    We skip those here and install the matching CPU-only torch build in step 2, so pyproject.toml
#    and uv.lock stay untouched. torch's pure-Python deps (sympy, networkx, ...) still come from the lock.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    skip="--no-install-package torch --no-install-package c2pa-python"; \
    for pkg in $(sed -nE 's/^name = "((nvidia-|cuda-)[^"]+|triton)"$/\1/p' uv.lock); do \
        skip="$skip --no-install-package $pkg"; \
    done; \
    uv sync --frozen --no-dev --extra ml --no-install-project $skip

# 2) CPU-only torch, same version as the lockfile. Its dependencies were installed in step 1.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    torch_version=$(sed -n '/^name = "torch"$/{n;s/^version = "\(.*\)"$/\1/p;}' uv.lock); \
    uv pip install --python /app/.venv/bin/python --no-deps \
        --index-url https://download.pytorch.org/whl/cpu "torch==${torch_version}"; \
    /app/.venv/bin/python -c "import torch; assert not torch.cuda.is_available(); print('torch', torch.__version__)"

# 2b) c2pa-python: the locked 0.38.2 was published with macOS wheels only, so it cannot install on
#     Linux. Try the locked version first, then fall back to C2PA_FALLBACK (0.38.1 has Linux wheels and
#     passes the C2PA test suite). Its deps (cryptography, requests, toml) came from the lock in step 1.
#     Remove this once pyproject/uv.lock pin a version with Linux wheels.
ARG C2PA_FALLBACK=0.38.1
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    c2pa_version=$(sed -n '/^name = "c2pa-python"$/{n;s/^version = "\(.*\)"$/\1/p;}' uv.lock); \
    uv pip install --python /app/.venv/bin/python --no-deps "c2pa-python==${c2pa_version}" \
      || uv pip install --python /app/.venv/bin/python --no-deps "c2pa-python==${C2PA_FALLBACK}"; \
    /app/.venv/bin/python -c "from c2pa import Context, Reader; print('c2pa ok')"

# 3) Bake the detector weights into the image so the container never needs the network.
ARG BTF_MODEL_ID=buildborderless/CommunityForensics-DeepfakeDet-ViT
ENV BTF_MODEL_ID=${BTF_MODEL_ID}
RUN /app/.venv/bin/python -c "\
import os; \
from transformers import AutoImageProcessor, AutoModelForImageClassification; \
mid = os.environ['BTF_MODEL_ID']; \
AutoImageProcessor.from_pretrained(mid); \
AutoModelForImageClassification.from_pretrained(mid); \
print('cached', mid)"

# 4) Application code.
COPY app ./app
COPY static ./static

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin btf \
    && mkdir -p /data \
    && chown -R btf:btf /data /opt/hf

ENV PATH="/app/.venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    BTF_DB_PATH=/data/banthefake.db

USER btf
VOLUME /data
EXPOSE 8000

# Run the venv's uvicorn directly (not `uv run`, which would re-sync and pull CUDA torch back in).
# One worker: the rate limiter is in-memory per process. Trusted proxy addresses must be set
# to your proxy's address: uvicorn reads FORWARDED_ALLOW_IPS (default 127.0.0.1, i.e. headers from
# other hosts are ignored). --no-access-log keeps client IPs out of the logs.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "1", "--no-access-log"]
