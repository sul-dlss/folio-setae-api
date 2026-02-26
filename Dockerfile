FROM tiangolo/uvicorn-gunicorn-fastapi:python3.11

RUN pip install poetry

COPY ./pyproject.toml /app/

RUN poetry add json2xml
RUN poetry install --without dev

COPY ./app /app

# Send print & log statements directly to stdout
ENV PYTHONUNBUFFERED 1
