FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock* ./
COPY . .

RUN uv sync --no-dev

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
