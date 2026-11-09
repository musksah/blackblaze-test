# SPEC: B2 File Store + AI Description Service

## 1. Goal

A small Python service that:

1. Accepts a file (browser upload, or a local path on the server) and stores it in Backblaze B2.
2. After storing it, generates a short AI description (a caption for images, a summary for text/PDF), attaches it to the stored object as metadata, and returns it to the caller with the file name, size, and status.

Interfaces: a curl-able JSON API plus one minimal HTML page.

## 2. Non-goals

- Authentication and multi-user support
- A database. B2 object metadata is the only persistence.
- Frontend frameworks or a build step. One static HTML file with inline JS.
- Large-file or multipart uploads (capped by `MAX_UPLOAD_MB`).

## 3. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.10+ |
| Web framework | FastAPI + Uvicorn (`python-multipart` for uploads) |
| B2 storage | `boto3` against B2's S3-compatible API |
| AI | `anthropic` Python SDK (Messages API) |
| Config | `python-dotenv`, values loaded from `.env` |
| Images (optional) | `Pillow`, to downscale images over the API size limit |
| Tests | `pytest`, with `botocore.stub.Stubber` and a fake Anthropic client |

## 4. Configuration

All secrets come from environment variables. **No secrets in source code, SPEC, or git.**

```env
# .env.example
B2_ENDPOINT=https://s3.us-east-005.backblazeb2.com
B2_REGION=us-east-005
B2_BUCKET=ai-interview-sebastian-huerfano
B2_KEY_ID=
B2_APPLICATION_KEY=
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-opus-5-5
MAX_UPLOAD_MB=20
LOCAL_UPLOAD_DIR=./samples        # only files under this dir may be ingested by path
```

`.gitignore` must include `.env`. The current `readme.md` contains live credentials. Move them into `.env`, and keep `readme.md` out of version control or strip the keys from it.

## 5. Request flow

```
client ──► POST /api/files ──► validate (size, type, name)
                                  │
                                  ▼
                       1. PutObject to B2      ── fails ──► 502, status="failed"
                          key = uploads/<uuid>-<safe_name>
                                  │
                                  ▼
                       2. describe(bytes, content_type) via Claude
                          ── unsupported / AI error ──► 200, status="stored"
                                  │                       (no description; error noted)
                                  ▼
                       3. CopyObject key→key, MetadataDirective=REPLACE
                          Metadata={"ai-description": ..., ...}
                                  │
                                  ▼
                       200, status="described"
```

The flow uploads first and describes second, as the task requires. A failure in the AI step must not lose the file: the object stays stored and the response says the description is missing.

## 6. API contract

### `GET /`
Serves `static/index.html`.

### `POST /api/files`: multipart upload
```bash
curl -F "file=@cat.jpg" http://localhost:8000/api/files
```

### `POST /api/files/local`: ingest a server-side path
```bash
curl -H "Content-Type: application/json" \
     -d '{"path": "samples/notes.txt"}' http://localhost:8000/api/files/local
```
The path is resolved and must fall inside `LOCAL_UPLOAD_DIR`. Otherwise the service returns 400, which blocks path traversal such as `../../etc/passwd`.

### `GET /api/files/{key:path}`: read back metadata (verification)
Calls `HeadObject` and returns the stored metadata, which proves the description was attached.

### `GET /healthz`
Returns `{"ok": true}`.

### Response body (both POST endpoints)
```json
{
  "file_name": "cat.jpg",
  "key": "uploads/3f1c…-cat.jpg",
  "size_bytes": 482113,
  "content_type": "image/jpeg",
  "kind": "image",
  "status": "described",
  "description": "A ginger cat asleep on a sunny windowsill next to a potted plant.",
  "error": null
}
```

| `status` | HTTP | Meaning |
|---|---|---|
| `described` | 200 | Stored, with the description attached as metadata |
| `stored` | 200 | Stored, but no description (unsupported type or AI failure). `error` says why. |
| `failed` | 4xx/5xx | Not stored. 400 for bad input, 413 if too large, 502 if B2 failed. |

## 7. AI description rules

**Type detection:** use the upload's content type, falling back to `mimetypes.guess_type`, and check the first bytes ("magic bytes") for images.

| Kind | Types | Sent to Claude as | Prompt goal |
|---|---|---|---|
| image | jpeg, png, gif, webp | `image` block (base64) | One-sentence caption, ≤ 200 chars |
| text | `text/*`, `.md .csv .json .py .log …` | Text in the user message (UTF-8, `errors="replace"`) | 1–2 sentence summary, ≤ 300 chars |
| pdf | `application/pdf` | `document` block (base64) | 1–2 sentence summary, ≤ 300 chars |
| other | anything else | Not sent | `status="stored"`, `error="unsupported type"` |

**Claude call:**
- Model: `ANTHROPIC_MODEL`, default `claude-opus-5-5`. The model is configurable, so a cheaper model such as `claude-haiku-5-5` can be swapped in for captions.
- `output_config={"effort": "low"}`. This is a simple task. Don't send a `thinking` param, because Opus 5.5 can't disable thinking.
- `max_tokens` around 4000, to leave room for thinking. Use a system prompt that asks for plain text only, with no preamble.
- Refusal fallback: `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`.
- Check `stop_reason` before reading content. On `"refusal"`, return `status="stored"` with an error.
- Errors: catch the SDK's typed exceptions (`RateLimitError`, `APIStatusError`, `APIConnectionError`) and map them to `status="stored"`. Never return a 500 because of the AI step.
- Oversized input: downscale images above about 5 MB with Pillow. If a text file is too large, the response includes `"truncated": true`; never truncate silently.

## 8. B2 storage details

- `boto3.client("s3", endpoint_url=B2_ENDPOINT, region_name=B2_REGION, aws_access_key_id=B2_KEY_ID, aws_secret_access_key=B2_APPLICATION_KEY)`
- **Key:** `uploads/<uuid4>-<sanitized filename>`. Sanitizing keeps `[A-Za-z0-9._-]` and caps the name at 100 chars.
- **Attaching metadata after upload:** S3 metadata can't be edited in place, so use `copy_object(CopySource={bucket,key}, Key=key, MetadataDirective="REPLACE", ContentType=<original>, Metadata=…)`.
- **Metadata keys:** `ai-description`, `ai-model`, `original-name`, `kind`.
- **Header safety:** metadata travels as `x-amz-meta-*` HTTP headers, so values must be ASCII. Percent-encode values with `urllib.parse.quote` and decode on read. Keep the total metadata under 2 KB (B2's S3 limit); the length caps in §7 ensure this.

## 9. Project layout

```
.
├── app/
│   ├── main.py          # FastAPI app, routes
│   ├── config.py        # env loading / settings
│   ├── storage.py       # B2: upload, set_metadata, head
│   ├── describer.py     # type detection + Claude call
│   └── service.py       # orchestrates upload → describe → attach
├── static/index.html    # minimal UI
├── samples/             # sample files for the local-path endpoint
├── tests/
├── .env.example
├── requirements.txt
└── RUNNING.md           # how to run + curl examples (no secrets; README.md would collide with readme.md on macOS)
```

## 10. Minimal HTML page

A single `index.html` with no CSS framework:
- A file `<input>` and an "Upload" button that sends `fetch("/api/files", {method:"POST", body: FormData})`
- A text input for a local path, with a button that posts to `/api/files/local`
- A result box showing name, size (human-readable), status, and description, or the error
- A "Working…" indicator while the request runs

## 11. Task breakdown

Each task is small and can be checked on its own.

| # | Task | Done when |
|---|---|---|
| 1 | **Scaffold:** venv, `requirements.txt`, layout from §9, `.gitignore`, `.env.example` | `uvicorn app.main:app` starts and `/healthz` returns ok |
| 2 | **Config:** `config.py` loads and validates the env vars and fails fast if any are missing | A missing key gives a clear startup error |
| 3 | **B2 connectivity:** `storage.py` client, plus a one-off script that uploads and lists a test object | The test object is visible in the bucket |
| 4 | **Storage functions:** `upload(bytes, key, content_type)`, `set_metadata(key, dict)` (copy-in-place), `head(key)` | Unit tests pass with `Stubber`, and the manual round-trip shows the metadata |
| 5 | **Type detection:** `detect_kind(name, content_type, head_bytes)` returns image, text, pdf, or other | Unit tests cover each kind and a mislabeled file |
| 6 | **Describer: text.** Claude call for text, with the prompt, length cap, and refusal and error handling | Returns a summary for `samples/notes.txt` |
| 7 | **Describer: image and PDF.** Add image blocks (with Pillow downscale) and PDF document blocks | Returns a caption for a jpg and a summary for a pdf |
| 8 | **Service orchestration:** upload → describe → attach → result, with the status rules from §6 | Unit tests cover the success, AI-failure, and unsupported-type paths |
| 9 | **Upload endpoint:** `POST /api/files` with size limit (413) and filename sanitizing | The curl example returns the §6 JSON |
| 10 | **Local-path endpoint:** `POST /api/files/local` with the `LOCAL_UPLOAD_DIR` guard | The traversal attempt returns 400 and a valid path works |
| 11 | **Metadata read-back:** `GET /api/files/{key}` | Shows the decoded `ai-description` from B2 |
| 12 | **HTML page:** `static/index.html` per §10 | Upload from a browser shows the description |
| 13 | **RUNNING.md:** setup, run, curl examples, design notes | A fresh clone runs by following it |

## 12. Iteration ideas (if time allows)

- Accept several files in one request and describe them concurrently with `asyncio.gather` and the async SDK client.
- A `GET /api/files` endpoint that lists recent uploads with their descriptions, plus a list view in the HTML page.
- Structured output (`output_config.format`) that returns `{description, tags[]}` and stores the tags as metadata.
- Retry or backfill: re-describe objects that have `status="stored"`.
- Image previews in the UI from presigned URLs (`generate_presigned_url`).
- Stream large uploads to B2 instead of reading them fully into memory.
- A Dockerfile so the service runs with one command.
