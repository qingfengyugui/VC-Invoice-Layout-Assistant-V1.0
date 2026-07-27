FROM maven:3.9.16-eclipse-temurin-17 AS wheel-build

WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/invoice-build \
    && /opt/invoice-build/bin/pip install --no-cache-dir --upgrade pip build

COPY pyproject.toml ./
COPY src ./src
COPY tools ./tools
RUN rm src/invoice_layout/bin/ofd-renderer.jar \
    && /opt/invoice-build/bin/python -m build --wheel \
    && /opt/invoice-build/bin/python -c "import glob, zipfile; wheel=glob.glob('/build/dist/*.whl'); assert len(wheel) == 1; archive=zipfile.ZipFile(wheel[0]); assert 'invoice_layout/bin/ofd-renderer.jar' in archive.namelist()"


FROM python:3.13.14-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-jre-headless \
        fonts-noto-cjk \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        unar \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheel-build /build/dist/ /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

WORKDIR /work
ENTRYPOINT ["invoice-layout"]
