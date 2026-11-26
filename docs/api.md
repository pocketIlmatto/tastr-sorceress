# tastr-sorceress API contract, v1 (2026-11-25)

The HTTP contract between a Tastr site and this helper. The site implements the same contract, so change it only
together with the site.

## Basics
- Base URL: `https://<tastr host>/pub/sorceress/v1`. Routes under `/pub/` are public; auth is the helper key.
- Helper auth: `Authorization: Bearer <key>` on everything except `pair/*`. Key = 32 random bytes, urlsafe base64,
  prefixed `sor_`. Server stores only sha256(key). Unknown or revoked key -> 401 `{"error":"unpaired"}`; the helper
  stops, deletes its key and starts pairing again. A person the site's admin has switched off -> 403
  `{"error":"not_allowed"}` on every keyed route; the helper keeps its key and asks again much later (it works once
  they are switched back on).
- Every link the site queues passes its allowlist, which also refuses a backslash, whitespace or control characters
  anywhere, a host with characters other than letters, digits, dots and hyphens, userinfo, and ports other than 443.
  The helper checks the same.
- JSON in and out (except part bodies). Errors: `{"error": "<code>", "detail": "<sentence>"}`.
- Off unless the site has helpers switched on: then every route answers 404.
- User-Agent: `tastr-sorceress/<version>`.

## Pairing (device-code flow)
1. `POST /pair/start` `{"name": "Pi in the hall", "version": "0.1.0"}` ->
   `{"pairing_id": "...", "poll_secret": "...", "code": "HXKM-4TQR", "expires_s": 600, "confirm_path": "/bank/sorceress"}`.
   Code: 8 chars from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, shown as XXXX-XXXX. Rate limit: 10 starts per IP per hour (429).
2. The person, signed in, enters the code on the site's `confirm_path` page (and may rename the helper there). The
   site refuses an unknown or expired code, and refuses people its admin hasn't allowed to pair.
3. Helper polls `POST /pair/poll` `{"pairing_id", "poll_secret"}` every 3 s ->
   202 `{"state": "waiting"}` | 200 `{"state": "paired", "key": "sor_...", "helper_id": "..."}` (returned ONCE; the
   pairing row is then deleted) | 410 `{"state": "expired"}`.

The site's page lists the person's own helpers (name, created, last seen, version, yt-dlp version, out of date) and
can remove one, which revokes its key at once. The site's admin decides who may pair and can change limits per person.

## Jobs
- `GET /jobs/next?wait=25` -> long-poll up to `wait` s (max 25). 204 no job, or 200:
  ```
  {"job_id": "...", "url": "https://www.youtube.com/watch?v=...", "purpose": "moment|track|capture",
   "lease_s": 900, "limits": {"max_seconds": 10800, "max_bytes": 1073741824, "part_bytes": 33554432},
   "allow_hosts": ["youtube.com", ...],
   "hints": {"title": "...", "artist": "...", "duration_s": 213}}
  ```
  `hints` is optional, and so is each field in it: what the site knows about the track, for helpers set up to search
  for a song (in a library or another service) instead of fetching the link. The link is still the job and passes
  the allowlist first either way.
  Taking a job leases it to this helper for `lease_s`; an expired lease returns the job to the queue (max 3 attempts, then failed).
  Only the key owner's jobs are ever returned.
- Server allowlist = helper allowlist (both enforce; https only; host equals or ends with "." + entry):
  `youtube.com, youtu.be, music.youtube.com, soundcloud.com, bandcamp.com, mixcloud.com`.
- `PUT /jobs/<id>/parts/<n>` (n = 1, 2, ...; body = raw bytes, `Content-Type: application/octet-stream`,
  `Content-Length` required, <= part_bytes; every part but the last >= 5 MiB, the storage's multipart minimum) ->
  200 `{"n": 1, "bytes": 33554432}`. 409 `not_your_job` / `lease_lost`; 413 `too_big` (part or running total > max_bytes; a total found over it once parts sent at the same time are recorded fails the job); 400 `bad_part` for a part number past what max_bytes can need.
  Re-sending part n replaces it. Each accepted part renews the lease for another `lease_s`. The server streams the body into a multipart upload; it never buffers a whole part.
- `POST /jobs/<id>/done` `{"parts": 3, "bytes": 81234567, "sha256": "<hex>", "ext": "webm|m4a|opus|mp3|...",
  "info": {"id", "extractor", "title", "duration", "uploader", "artist", "webpage_url", "upload_date",
  "source", "steps"}}` ->
  200 `{"deposit_id": "..."}`. 422 `size_mismatch` when bytes != sum of parts. The server completes the multipart
  upload and continues as for an uploaded file. sha256 is stored, not re-checked.
  `info.source` (string) names the source that found the audio (`yt-dlp` unless the person set up another) and
  `info.steps` (list of strings) the steps that ran on the file afterwards, in order; both are optional, short
  names from the helper's own config, for display.
- `POST /jobs/<id>/failed` `{"reason": "bot_check|unavailable|too_long|too_big|not_allowed|live|playlist|error",
  "detail": "<= 500 chars>"}` -> 200. The server aborts the multipart upload and shows the reason on the page.
- `POST /heartbeat` `{"version": "0.1.0", "yt_dlp": "2026.11.23"}` every 5 min -> 200
  `{"min_version": "0.1.0", "latest_version": "0.1.0"}`; the page marks a helper out of date when yt-dlp is > 30 days old.

## Limits (defaults; per-person admin override)
20 jobs queued per person per UTC day (refused at queue time with 429 on the app route), 3 h and 1 GB per link,
a job queued 7 days without completing fails `helper_offline`. Fetched audio counts toward the storage cap.
