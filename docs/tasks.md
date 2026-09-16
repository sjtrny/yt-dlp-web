# Tasks

A Task checks one URL on a CRON schedule. Its mode sets when a download starts.

## Controls

Open `/tasks`. Fill in **New**, then select **Add**.

| Control | Action |
| --- | --- |
| Save | Store changes and calculate the next run |
| Run | Check now with the saved settings |
| Enabled | Allow automatic checks; select Save to apply |
| Delete | Remove the Task |

Save changes before selecting **Run**. Run does not save form changes.
It also works on a disabled Task and does not change the next scheduled time.

## Fields

| UI label | API field | Default | Meaning |
| --- | --- | --- | --- |
| Name | `name` | URL, up to 120 characters | Display name; 120 characters maximum |
| URL | `url` | Required | Video or live-stream page |
| CRON | `cron` | `*/5 * * * *` | Five-field schedule |
| TZ | `timezone` | `YTDLP_DEFAULT_TIMEZONE`, or `UTC` | IANA time zone, such as `Australia/Sydney` |
| Mode | `mode` | `live` | Live: `live`; Always: `download` |
| Enabled | `enabled` | `true` | JSON boolean; false disables automatic checks |

Set `YTDLP_DEFAULT_TIMEZONE` before starting the app to pre-fill **TZ** for new
Tasks. It also applies when an API request omits `timezone`. Existing Tasks
keep their saved timezone. See [Configuration](../README.md#configuration).

### Modes

- **Live**: Check metadata first. Start only if yt-dlp reports a live stream.
  The check has a 60-second limit. The worker checks live status again before
  download. If the stream ends, it does not download the recorded video.
- **Always**: Submit the URL at each check without a live-status check.
  Normal download errors can still occur.

An offline or upcoming stream does not start a Live-mode download.
An extraction failure is an error, not an offline result.
Live detection depends on the site's yt-dlp extractor.

## CRON

Field order: `minute hour day-of-month month day-of-week`.

| Expression | Schedule |
| --- | --- |
| `*/5 * * * *` | Every five minutes |
| `0 * * * *` | Every hour |
| `0 9 * * *` | Daily at 09:00 |
| `0 9 * * mon-fri` | Monday to Friday at 09:00 |

Times use the Task's time zone. Use `UTC` to avoid daylight-saving changes.
Displayed and API timestamps use UTC, with an ISO 8601 `Z` suffix.

The schedule accepts numbers, lists, ranges, steps, and three-letter month or
weekday names. If both day fields are restricted, either field can match.
There is no seconds field, shell command, or random schedule.
The expression must have a match within eight years and at most 200 characters.

The scheduler always starts with the app. Missed intervals produce one check,
not one check per interval. Checks are not queued.

## Duplicate rules

- One Task cannot run two checks at the same time.
- Tasks, API requests, and the web form share one active download per normalized
  URL. The lock remains through startup, recording, Stop, and finalization.
- A later check can start another download after the worker exits.
  Tasks do not track which broadcasts were recorded before.
- Different aliases or query strings can be different URLs.
  See [URL rules](api.md#url-rules).

## Pause, edit, and delete

Clear **Enabled** and select **Save** to pause a Task. This cancels its current
check before it can start a download. Edits and deletion do the same.
They do not stop a download that has already started.

To stop without a later automatic restart, pause the Task first. Then use
**Stop** on Downloads. Pause any other Task that checks the same URL too.

## Results

| `last_result` | Meaning |
| --- | --- |
| `never` | No check yet |
| `checking` | Check in progress |
| `offline` | No live stream found |
| `started` | New download started |
| `already_running` | Existing active download reused |
| `error` | Check failed; see `last_error` |
| `cancelled` | Task changed or scheduler stopped during the check |
| `interrupted` | App restarted before the check finished |

`started` does not mean the download is complete. Follow `last_job_id` to get
the job result. An error does not disable the schedule.

Tasks and results stay in the state directory. Disabled Tasks have no next-run
time. After restart, enabled Tasks continue; unfinished checks become
`interrupted`. See [State and restart](../README.md#state-and-restart).

Use the [Task API](api.md#tasks) to create, update, run, or delete Tasks.
