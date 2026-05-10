FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy source and install
COPY pyproject.toml ./
COPY bot ./bot
RUN pip install --no-cache-dir .

# Copy remaining files (schema.sql, etc.)
COPY . .

CMD ["python", "-m", "bot.main"]
