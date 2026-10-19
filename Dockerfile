FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stopproof/ stopproof/
COPY demo/ demo/
COPY README.md pytest.ini ./

# The Nebius backend executes inside remote microVMs, so this container only
# orchestrates (Nemotron API + Nebius Sandbox API + self-hosted live inbox).
# The local bubblewrap fallback needs kernel namespace features most container
# hosts deny — keep STOPPROOF_SANDBOX=nebius for cloud deployments.
ENV PYTHONUNBUFFERED=1 STOPPROOF_SANDBOX=nebius
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5)"

# Secrets come from the environment at run time (never baked in):
#   docker run --env-file .env -v stopproof-runs:/app/runs -p 8000:8000 stopproof
CMD ["python", "-m", "stopproof", "serve", "--host", "0.0.0.0", "--port", "8000"]
