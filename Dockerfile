FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 app && mkdir /scratch && chown app /scratch
USER app
# Root fs is read-only: uploads >1 MB are spooled to a temp file, so TMPDIR must be writable too.
ENV PDF_SPLITTER_DATA=/scratch TMPDIR=/scratch

EXPOSE 8000
# Single worker: job state is held in memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
