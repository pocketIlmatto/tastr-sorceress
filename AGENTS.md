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

The person can change how their helper fetches with a config file: other sources, and steps that run on the file
before upload ([README](README.md#customising-your-fetcher)). Custom sources and steps are that person's own opt-in,
on their own machine and their own responsibility. The rules below describe what the helper does by default, and the
guardrails that hold whatever the config says.

By default, with no config file:

- It talks only to the configured Tastr site (`TASTR_URL`, https only) and the music sites it fetches from: no
  storage, compute or telemetry services, no redirects followed on calls to the site, no connections from a fetch
  to private or local addresses (yt-dlp may follow the music sites' redirects to their media hosts), no open ports.
- It fetches with the built-in `yt-dlp` source only, limited to the extractors for allowlisted sites (never the
  generic extractor), with no postprocessors and no ffmpeg. A search job (no link) runs yt-dlp's YouTube search
  with YouTube's extractors only, and the link it picks then goes through the allowlist like any other. yt-dlp plugins stay off (`YTDLP_NO_PLUGINS`), whatever
  the config says.
- Nothing is kept but the key.
- The default image has no ffmpeg; a person who wants the `convert` step builds their own image (`examples/`).

Always, for every source and step, built-in or not (`tastr_sorceress/pipeline.py`, `worker.py`):

- The allowlist check comes first and is enforced locally as well as on the site (for a search job, on the link the
  search picked, before anything is downloaded). A source that searches instead of fetching the link gets the same
  job, after the same check.
- What the helper logs during a job is sent to the site's job log (`joblog.py`): only the job thread's lines, never
  the key, best effort, and never in the way of the job.
- Every plugin call runs under the private-address guard; only hosts the person lists in `[network] local_hosts` are
  exempt, and never for yt-dlp itself.
- Before upload the file is re-checked: a plain file inside the job's folder, within `max_bytes`, within
  `max_seconds` when the length is known, and a type the site takes. Only the info fields in docs/api.md are sent.
- Temporary files are deleted after every job, whatever the outcome; only the `copy` step's folder keeps anything.
- Plugins never get the key. Plugins are found only through the `tastr_sorceress.sources` and
  `tastr_sorceress.steps` entry points and loaded only when the config names them; a name that isn't installed
  stops the helper at start.
- The package is standard library plus yt-dlp only; the image adds what yt-dlp itself needs (its default extras and
  Deno for YouTube). No new runtime dependencies without a good reason; add any with terms of their own to
  [NOTICE](NOTICE). Don't ship plugins for other download services in this repository; the example in `examples/`
  stays a local-folder example.
- Back off on errors; never hammer the site.
- Workflows: least-privilege `permissions:` per job, third-party actions pinned by full commit SHA, and never
  interpolate `${{ github.event.* }}` text into `run:`.
