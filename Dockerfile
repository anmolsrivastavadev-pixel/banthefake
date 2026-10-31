# syntax=docker/dockerfile:1
# BanTheFake: FastAPI app + two CPU-only AI-image detectors, runs offline at runtime.
# Base images are pinned by multi-arch index digest (linux/amd64 + linux/arm64); the tag is informational.
# To update: look up the new index digest for the tag and replace both the tag comment and the digest.
# python:3.12-slim
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# ghcr.io/astral-sh/uv:0.12.5
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf

WORKDIR /app

# 1) Locked dependencies, including the `ml` and `faceswap` extras, EXCEPT torch and its CUDA stack.
#    uv.lock pins torch from PyPI, whose Linux wheel pulls ~4 GB of nvidia/cuda/triton packages.
#    We skip those here and install the matching CPU-only torch build in step 2, so pyproject.toml
#    and uv.lock stay untouched. torch's pure-Python deps (sympy, networkx, ...) still come from the lock.
#    Everything else, c2pa-python included (pinned to a release with Linux wheels), installs from the lock.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    skip="--no-install-package torch"; \
    for pkg in $(sed -nE 's/^name = "((nvidia-|cuda-)[^"]+|triton)"$/\1/p' uv.lock); do \
        skip="$skip --no-install-package $pkg"; \
    done; \
    uv sync --frozen --no-dev --extra ml --extra faceswap --no-install-project $skip; \
    /app/.venv/bin/python -c "from c2pa import Context, Reader; print('c2pa ok')"

# 2) CPU-only torch, same version as the lockfile, hash-pinned (download.pytorch.org is not covered by
#    uv.lock). Its dependencies were installed in step 1. The hashes are the cp312 manylinux_2_28 wheels
#    from https://download.pytorch.org/whl/cpu/torch/ for x86_64 and aarch64; when uv.lock moves torch,
#    the version check below fails the build until TORCH_CPU_VERSION and both hashes are updated.
ARG TORCH_CPU_VERSION=2.15.0
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    torch_version=$(sed -n '/^name = "torch"$/{n;s/^version = "\(.*\)"$/\1/p;}' uv.lock); \
    if [ "$torch_version" != "$TORCH_CPU_VERSION" ]; then \
        echo "uv.lock has torch $torch_version but the Dockerfile pins CPU wheels for $TORCH_CPU_VERSION" >&2; exit 1; \
    fi; \
    echo "torch==${TORCH_CPU_VERSION}+cpu" \
        "--hash=sha256:0955c7e3446326293f48af18fad67872fa31dee5a36a87690eb9e5be8b465be3" \
        "--hash=sha256:75941d429aaf4450bcd364345d7677e2d54aad45b38fbfcb1c0552450a6e9c40" \
        > /tmp/torch-cpu.txt; \
    uv pip install --python /app/.venv/bin/python --no-deps --require-hashes \
        --index-url https://download.pytorch.org/whl/cpu -r /tmp/torch-cpu.txt; \
    rm /tmp/torch-cpu.txt; \
    /app/.venv/bin/python -c "import torch; assert not torch.cuda.is_available(); print('torch', torch.__version__)"

# 3) Bake BOTH detector weights into the image so the container never needs the network.
#    Primary (H) and secondary (C) models; custom ids need their *_REVISION set for reproducible builds.
ARG BTF_MODEL_ID=haywoodsloan/ai-image-detector-deploy
ARG BTF_MODEL_REVISION=
ARG BTF_MODEL2_ID=buildborderless/CommunityForensics-DeepfakeDet-ViT
ARG BTF_MODEL2_REVISION=
ENV BTF_MODEL_ID=${BTF_MODEL_ID} \
    BTF_MODEL_REVISION=${BTF_MODEL_REVISION} \
    BTF_MODEL2_ID=${BTF_MODEL2_ID} \
    BTF_MODEL2_REVISION=${BTF_MODEL2_REVISION}
RUN /app/.venv/bin/python - <<'PY'
import os
from transformers import AutoImageProcessor, AutoModelForImageClassification
pinned = {
    "haywoodsloan/ai-image-detector-deploy": "4ae1822603cb1ec980c217cfde1ef1b8e0477a16",
    "buildborderless/CommunityForensics-DeepfakeDet-ViT": "ac6ee457bea904a373065754107451793b56db00",
}
for var in ("BTF_MODEL", "BTF_MODEL2"):
    mid = os.environ[var + "_ID"]
    revision = os.environ[var + "_REVISION"] or pinned.get(mid)
    AutoImageProcessor.from_pretrained(mid, revision=revision)
    AutoModelForImageClassification.from_pretrained(mid, revision=revision, use_safetensors=True)
    print("cached", mid, revision)
PY

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
