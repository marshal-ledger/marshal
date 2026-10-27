# Marshal ingest API and web view. Releases are published as ghcr.io/marshal-ledger/marshal.
#   docker build -t marshal .
#   docker run -p 8080:8080 -v ./marshal-server.yaml:/etc/marshal/marshal-server.yaml:ro \
#     -e MARSHAL_STORE_URL=postgresql://... marshal
# It runs as any UID in group 0 with a read-only root filesystem; give it a writable /tmp and,
# for a SQLite store, a volume at /var/lib/marshal.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# azure: Microsoft Entra ID sign-in to Azure Database for PostgreSQL (store_auth: azure).
RUN pip install --no-cache-dir --prefix=/install ".[server,postgres,azure]"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="Marshal" \
      org.opencontainers.image.description="Marshal, a ledger for enterprise AI agents: ingest API and web view" \
      org.opencontainers.image.source="https://github.com/marshal-ledger/marshal" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY LICENSE /usr/share/licenses/marshal/LICENSE
# OpenShift runs containers as an arbitrary UID in group 0, so the user is in group 0 and the
# data directory is writable by the group. Nothing else in the image is written at run time.
RUN useradd --system --uid 10001 --gid 0 --no-create-home marshal \
 && install -d -o 10001 -g 0 -m 2770 /var/lib/marshal
COPY --from=build /install /usr/local
USER 10001:0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["marshal"]
CMD ["serve", "--config", "/etc/marshal/marshal-server.yaml", "--host", "0.0.0.0", "--port", "8080"]
