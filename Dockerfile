FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stopproof/ stopproof/
COPY demo/ demo/
COPY README.md pytest.ini ./

# The Nebius backend executes inside remote microVMs, so this container only
# orchestrates (model calls + sandbox API + ntfy sink). The local bubblewrap
# fallback needs kernel namespace features most hosts don't give containers —
# keep STOPPROOF_SANDBOX=nebius for deployments.
ENV PYTHONUNBUFFERED=1 STOPPROOF_SANDBOX=nebius
EXPOSE 8000

# Secrets come from the environment at run time (never baked in):
#   docker run --env-file .env -p 8000:8000 stopproof
CMD ["python", "-m", "stopproof", "serve", "--host", "0.0.0.0", "--port", "8000"]
