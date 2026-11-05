FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime \
    HOME=/home/welfare \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin welfare

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt \
    && python -m pip check \
    && python -m copilot download-runtime \
    && chmod -R a+rX /opt/copilot-runtime

COPY main.py mcp_server.py ./
COPY app ./app
COPY seed ./seed
COPY welfare ./welfare

USER welfare
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health',timeout=3).close()"]

CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
