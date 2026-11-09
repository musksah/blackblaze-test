# Running the B2 File Describer

The service stores a file in Backblaze B2. It then has Claude write a short caption (for an image) or summary (for text or a PDF), attaches that text to the stored object as metadata, and returns it. See [SPEC.md](SPEC.md) for the full design.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in the B2 and Anthropic keys
```

## Run

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000 for the web page, or use curl:

```bash
# Upload a file
curl -F "file=@samples/house.png" http://localhost:8000/api/files

# Store a file that is already on the server (must be inside LOCAL_UPLOAD_DIR, default samples/)
curl -H "Content-Type: application/json" -d '{"path": "samples/notes.txt"}' \
     http://localhost:8000/api/files/local

# Read the stored metadata back from B2
curl http://localhost:8000/api/files/uploads/<key-from-the-response>
```

Example response:

```json
{
  "file_name": "house.png",
  "key": "uploads/ea04ccf9f0c0-house.png",
  "size_bytes": 1866,
  "content_type": "image/png",
  "kind": "image",
  "status": "described",
  "description": "A simple flat illustration of a red house with a brown roof and dark door on green grass under a light blue sky with a yellow sun.",
  "model": "claude-opus-5-5",
  "input_truncated": false,
  "error": null
}
```

`status` takes one of three values:
- `described`: the file is stored and the description is attached to it.
- `stored`: the file is stored but has no description, because the type is unsupported or the AI step failed. `error` says which.
- `failed`: the file was not stored.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests run offline. B2 is replaced with botocore's `Stubber` and Claude with a fake client.

## Code layout

| File | Role |
|---|---|
| `app/main.py` | FastAPI routes and the HTML page |
| `app/service.py` | Upload, then describe, then attach the metadata |
| `app/storage.py` | B2 through the S3 API. Metadata is attached by copying the object onto itself with `MetadataDirective=REPLACE`. |
| `app/describer.py` | File-type detection and the Claude call |
| `app/config.py` | Settings read from `.env` |
| `static/index.html` | The web page |
