# FROM tiangolo/uvicorn-gunicorn-fastapi:python3.11-2024-11-18
FROM python:3.12-slim-bookworm

WORKDIR /app

RUN pip install poetry fastapi[standard]
RUN poetry config virtualenvs.create false

COPY ./pyproject.toml /app/

RUN poetry install --without dev

COPY ./app /app

# Send print & log statements directly to stdout
ENV PYTHONUNBUFFERED 1

CMD ["fastapi", "run", "main.py", "--port", "80"]
