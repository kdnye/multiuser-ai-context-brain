FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PORT=8080 MCP_TRANSPORT=http BRAIN_STORE=gcs
WORKDIR /app
COPY requirements-mcp.txt ./
RUN pip install -r requirements-mcp.txt
# Code only. The wiki lives in Cloud Storage; raw data, builds, private files and tests never enter the image.
COPY mcp_server ./mcp_server
COPY tools ./tools
RUN useradd --system --no-create-home app
USER app
EXPOSE 8080
CMD ["python", "-m", "mcp_server.server"]
