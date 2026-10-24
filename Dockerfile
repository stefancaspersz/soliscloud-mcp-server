FROM python:3.15-slim-trixie

LABEL org.opencontainers.image.description="My soliscloud MCP server" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/stefancaspersz/soliscloud-mcp-server"


WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py soliscloud_client.py ./

EXPOSE 8765

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
