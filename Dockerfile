FROM python:3.11-slim

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

WORKDIR /work
ENTRYPOINT ["carbon"]
