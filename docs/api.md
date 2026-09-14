# HTTP API v1

Base path: `/api/v1`. Send JSON objects with `Content-Type: application/json`.
The request body limit is 16 KiB. Unknown fields return 400.

If `YTDLP_API_TOKEN` is set, send `Authorization: Bearer TOKEN` on every route.
A signed browser session also works. Tokens in URLs are not accepted.
Cross-site browser writes are rejected.

Examples use `SERVER=http://localhost:8080`. Set `YTDLP_API_TOKEN` to the server
token. Omit the Authorization header if authentication is disabled.

## Routes

Paths below are relative to `/api/v1`. Braces identify path parameters.

| Method | Path | Success response |
| --- | --- | --- |
| GET | `/health` | 200: `status`, `api_version`, `scheduler_enabled` |
| POST | `/downloads` | 202: new job; 200: reused job |
| GET | `/downloads` | 200: `{"jobs": [...]}` |
| GET | `/downloads/{id}` | 200: `{"job": {...}}` |
| POST | `/downloads/{id}/stop` | 202: stopping; 200: job already ended |
| GET | `/downloads/{id}/file` | 200: media attachment |
| POST | `/tasks` | 201: `{"task": {...}}` |
| GET | `/tasks` | 200: `{"tasks": [...]}` |
| GET | `/tasks/{id}` | 200: `{"task": {...}}` |
| PATCH | `/tasks/{id}` | 200: `{"task": {...}}` |
| DELETE | `/tasks/{id}` | 204: empty body |
| POST | `/tasks/{id}/run` | 202: check started; 200: check already in progress |

## Downloads

```sh
SERVER=http://localhost:8080
curl -X POST "$SERVER/api/v1/downloads" \
  -H "Authorization: Bearer $YTDLP_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: request-001' \
  -d '{"url":"https://example.com/video"}'
```

`url` is required. No other body fields are accepted. The API has no per-request
yt-dlp options, output paths, or backend selection.

Example response:

```json
{
  "created": true,
  "job": {
    "id": "job-id",
    "url": "https://example.com/video",
    "title": "Loading…",
    "status": "starting",
    "progress": "0%",
    "live": false,
    "can_stop": false,
    "task_id": null,
    "error": null,
    "created_at": "2026-09-14T10:00:00Z",
    "finished_at": null,
    "status_url": "/api/v1/downloads/job-id",
    "download_url": null
  }
}
```

A 202 response means the job was accepted, not completed. A reused job returns
200 with `created: false`. Both responses set `Location` to `job.status_url`.
IDs and the initial state can differ from the example.

Poll `status_url` with GET. When `status` is `complete`, get `download_url`.
Both URLs are relative to the server. `download_url` is null before completion.
`error` is null unless the job failed or was interrupted. `task_id` identifies
the Task that created the job, or is null. A reused job keeps its original ID
and Task link.

Active states: `starting`, `downloading`, `recording`, `stopping`, `finalizing`.
Final states: `complete`, `failed`, `interrupted`.

`live` is false until metadata identifies a live stream. `can_stop` means Stop
is available now. `title` has at most 80 characters. `progress` is display text,
not a number. Times are UTC ISO 8601 strings. `finished_at` is null while active.

GET `/downloads` lists jobs by creation time, newest first. Use `?status=active`
or an exact state to filter the list. There is no pagination.

### URL rules

Accepts HTTP and HTTPS only, with no embedded credentials. The URL has at most
4096 characters after trimming. Spaces and control characters are not accepted.

Normalization removes surrounding spaces, fragments, and default ports.
It converts scheme and host to lowercase, converts Unicode host names to IDNA,
and adds `/` to an empty path. Paths and query strings are preserved.
Different site aliases are not matched by video ID.

### Duplicate and retry rules

Checks run in this order:

1. If the request key exists, return its original job. A different normalized
   URL with the same key returns 409.
2. If the URL is active, return that job. This also applies to jobs from the UI
   or Tasks. A new request key is stored against that job.
3. Otherwise, create a job and store the optional request key.

`Idempotency-Key` is optional. It accepts 1–128 ASCII characters. Use one key
per submission and reuse it for retries. Keys and their job links survive
completion and restart. A known key returns its original job even if a newer
job is active for that URL.

Without a known key, a request after the earlier job ends starts a new job.
Active URL protection continues until the worker exits, including finalization.

### Stop

```sh
curl -X POST "$SERVER/api/v1/downloads/JOB_ID/stop" \
  -H "Authorization: Bearer $YTDLP_API_TOKEN"
```

No body is required. Wait for `can_stop: true` before the first request.
Stop returns `{"job": {...}}`. Repeat requests after an accepted Stop are safe.
A finished, failed, or interrupted job returns 200.

Stop returns 409 for an ordinary download, a recorder not yet ready, or natural
finalization. It does not force-cancel a job. Poll until the job ends.

## Tasks

```sh
curl -X POST "$SERVER/api/v1/tasks" \
  -H "Authorization: Bearer $YTDLP_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Live check","url":"https://example.com/channel/live","cron":"*/5 * * * *","timezone":"UTC","mode":"live","enabled":true}'
```

See [Task fields](tasks.md#fields) for defaults and limits.
POST creates a Task and sets `Location` to `/api/v1/tasks/{id}`.
PATCH accepts any subset of the same fields. Invalid fields leave the Task
unchanged. Every save calculates a new next-run time.

GET example:

```json
{
  "task": {
    "id": "task-id",
    "name": "Live check",
    "url": "https://example.com/channel/live",
    "cron": "*/5 * * * *",
    "timezone": "UTC",
    "mode": "live",
    "enabled": true,
    "created_at": "2026-09-14T10:00:00Z",
    "updated_at": "2026-09-14T10:00:00Z",
    "next_run_at": "2026-09-14T10:05:00Z",
    "last_checked_at": null,
    "last_result": "never",
    "last_error": null,
    "last_job_id": null,
    "checking": false
  }
}
```

POST and PATCH return the saved fields without `checking`. GET and Run include
`checking`. `last_checked_at` is the latest check's start time.
`last_job_id` is the job from the last finished check, or null if none was used.
It can still show the previous job while a new check runs.
`last_error` is null unless a check failed or was interrupted.
A disabled Task has `next_run_at: null`.

POST `/tasks/{id}/run` needs no body. It returns `{"started": true, "task": {...}}`
with 202. If a check is already in progress, it returns `started: false` with 200.
Poll the Task until `checking` is false. Run works on disabled Tasks and does
not change the next scheduled time.

To pause, PATCH `{"enabled": false}`. Pause, edit, and delete prevent the current
check from starting a new download. They do not stop an active download.
See [Task results](tasks.md#results) and [duplicate rules](tasks.md#duplicate-rules).

## Errors

```json
{"error":{"code":"invalid_request","message":"mode must be live or download"}}
```

| Status | Meaning |
| --- | --- |
| 400 | Invalid URL, JSON, field, or schedule |
| 401 | Missing or invalid token; includes `WWW-Authenticate: Bearer` |
| 403 | Cross-site browser write |
| 404 | Unknown job or Task, or missing completed file |
| 405 | Wrong HTTP method |
| 409 | Request key conflict, or Stop unavailable |
| 413 | Body exceeds 16 KiB |
| 415 | Wrong content type for a JSON request |
| 500 | Internal failure; check server logs |

Field validation uses `invalid_request`. Other codes follow the HTTP error
name, such as `not_found` or `conflict`. Messages give the error detail.
A download can fail after acceptance. That failure appears in the job, not as
a new HTTP error for the original request.
