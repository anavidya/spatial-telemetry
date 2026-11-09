# Backend image. Lives at the project root so the build context includes pyproject.toml/poetry.lock
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir "poetry>=2,<3"

WORKDIR /app

# Install dependencies first so this layer is cached until the lock file changes
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root && rm -rf /root/.cache/pypoetry

COPY backend/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
