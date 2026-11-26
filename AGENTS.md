# Notes for agents

Rules for anyone changing this repository, Claude and OpenAI Codex included. The repository is public.

## What this repository is

The tastr-sorceress helper: a Python package and Docker image that fetches audio from music links for one person's
account on a Tastr site. The only interface to the site is the HTTP API in [docs/api.md](docs/api.md).

- Don't mention private trackers, ticket numbers, or the Tastr app's internals beyond what docs/api.md says, in
  code, commits, pull requests, comments or docs. Describe the work instead.
- Never copy code, data, history or notes from the app's repository into this one.
- docs/api.md is shared with the site. Don't change the HTTP contract here alone; a change needs the site to change
  with it.

## Pull requests

- Open every pull request against `main`. Don't merge, tag, publish images by hand or change repository settings;
  say in the pull request what the owner needs to do.
- Run the tests before pushing: `python -m pip install -e '.[test]'` then `python -m pytest -q`. Never skip, weaken
  or delete a test to get a green run.
- Never commit secrets, keys, tokens or real people's emails. Tests use made-up keys and example.com addresses.
- Before pushing, check that `origin` is `pocketIlmatto/tastr-sorceress`.

## Rules for the helper

- It talks only to the configured Tastr site (`TASTR_URL`, https only) and the music sites it fetches from: no
  storage, compute or telemetry services, no redirects followed on calls to the site, no connections from a fetch
  to private or local addresses (yt-dlp may follow the music sites' redirects to their media hosts), no open ports.
- The allowlist check comes first and is enforced locally as well as on the site; yt-dlp stays limited to the
  extractors for allowlisted sites (never the generic extractor), with no postprocessors and no ffmpeg.
- Nothing is kept but the key: temporary audio is deleted after every job, whatever the outcome.
- Standard library plus yt-dlp only in the package; the image adds what yt-dlp itself needs (its default extras and
  Deno for YouTube). No new runtime dependencies without a good reason; add any with terms of their own to
  [NOTICE](NOTICE). yt-dlp plugins stay off (`YTDLP_NO_PLUGINS`).
- Back off on errors; never hammer the site.
- Workflows: least-privilege `permissions:` per job, third-party actions pinned by full commit SHA, and never
  interpolate `${{ github.event.* }}` text into `run:`.
