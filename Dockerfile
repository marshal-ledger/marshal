# Marshal ingest API and web view. Releases are published as ghcr.io/marshal-ledger/marshal.
#   docker build -t marshal .
#   docker run -p 8080:8080 -v ./marshal-server.yaml:/etc/marshal/marshal-server.yaml:ro \
#     -e MARSHAL_STORE_URL=postgresql://... marshal
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[server,postgres]"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="Marshal" \
      org.opencontainers.image.description="Marshal, a ledger for enterprise AI agents: ingest API and web view" \
      org.opencontainers.image.source="https://github.com/marshal-ledger/marshal" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY LICENSE /usr/share/licenses/marshal/LICENSE
RUN useradd --system --uid 10001 --no-create-home marshal
COPY --from=build /install /usr/local
USER 10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["marshal"]
CMD ["serve", "--config", "/etc/marshal/marshal-server.yaml", "--host", "0.0.0.0", "--port", "8080"]
