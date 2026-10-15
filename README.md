# PDF Splitter

Teachers upload a large scanned PDF, fix pages scanned upside down, and download it split into
fixed-size files, zipped. Nothing is kept: every upload and generated zip is deleted 15 minutes
after upload (and the data folder is wiped on server start/stop).

## Run

```
pip install -r requirements.txt
PDF_SPLITTER_PIN=1234 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run a single worker (job state is in memory). Serve over HTTPS in production, since the pin is sent in the upload.

| Env var | Default | Meaning |
|---|---|---|
| `PDF_SPLITTER_PIN` | *required* | Pass pin teachers must enter |
| `PDF_SPLITTER_TTL_SECONDS` | 900 | Lifetime of an upload and its zips |
| `PDF_SPLITTER_MAX_UPLOAD_MB` | 500 | Max upload size |
| `PDF_SPLITTER_DATA` | `data` | Scratch directory |

## Use

1. Enter a user name, the pin and choose the PDF.
2. Thumbnails appear; use ↺ / ↻ on a page (or "Rotate all 180°") to fix orientation.
3. Set pages per file, and optionally a max zip size in MB. If the files don't fit one zip,
   several zips are produced, each under the limit (a single file bigger than the limit is rejected
   with a message to use fewer pages per file).

Wrong pins are throttled (5 failures per IP per minute). Tests: `pip install pytest && pytest`.

## Deploy with Docker Compose

```
cp .env.example .env     # set PDF_SPLITTER_PIN
docker compose up -d --build
```

Listens on `PORT` (default 8000). Put a reverse proxy with HTTPS in front (e.g. Caddy/nginx) and
raise its upload limit to match `PDF_SPLITTER_MAX_UPLOAD_MB` (nginx: `client_max_body_size`).
Scratch space is a RAM-backed tmpfs (`SCRATCH_SIZE`, default 4g, counts against memory only as used),
so files never touch disk and disappear if the container stops.

The container joins the external `dino-app` Docker network (it must already exist:
`docker network create dino-app` if the dino-app stack isn't running). Other containers on that
network can reach it at `http://pdf-splitter:8000`.
