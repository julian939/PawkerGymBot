FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first (layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY . .

CMD ["python", "-m", "bot.main"]
