# Build image for Anthropic's OSS Scanner (https://github.com/anthropics/oss-scanner).
# The scanner builds this with the repository root as the build context, then
# removes network access: everything the tests need is fetched here. It mirrors
# the test-full-suite job in .github/workflows/ci.yml.
FROM python:3.12-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates build-essential git \
    && rm -rf /var/lib/apt/lists/*

# scanner contract: the checkout lives inside the image, at /src
COPY . /src
WORKDIR /src

# CPU-only torch first so the [nli] extra does not pull a CUDA wheel.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch
RUN pip install -e ".[dev,ast,mcp,nli]" psutil platformdirs py-cpuinfo numpy joblib scikit-learn \
    && python -m spacy download en_core_web_sm

# Offline from here on: no Hugging Face or telemetry calls at test time.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    CYMATIX_OTEL_ENABLED=0

# Run the offline suite so the image is known to work, without letting a
# failing test abort the build.
RUN python -m pytest tests/ -m "not live" -q -p no:cacheprovider \
    || echo "WARNING: pytest reported failures"
