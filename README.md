# tastr-sorceress

A small helper you run in Docker on your own always-on hardware (a Raspberry Pi, a NAS, a Mac) that fetches the
audio of music links for your own account on a Tastr site.

## Why it exists

YouTube asks cloud servers to prove they're not bots, so a Tastr site can't reliably fetch a link itself. This
helper does the fetching from your home connection instead: you queue a link on the site, the helper picks it up,
fetches the audio with [yt-dlp](https://github.com/yt-dlp/yt-dlp) and uploads it to your account.

It talks only to your Tastr site's address and to the music sites it fetches from. It has no open ports and needs
no other accounts or services. The HTTP API it speaks is in [docs/api.md](docs/api.md).

## Setup (once)

1. Save [compose.yaml](compose.yaml) on the machine and set `TASTR_URL` to your Tastr site (for example
   `https://tastr.example.com`). Set `SORCERESS_NAME` to how you want the helper listed.
2. Start it:

   ```sh
   docker compose up -d
   ```

3. Read the pairing code it prints:

   ```sh
   docker compose logs sorceress
   ```

   You'll see something like:

   ```
   ================================================================
     Pair this helper with your Tastr account:
       1. Open   https://tastr.example.com/bank/sorceress
       2. Enter  HXKM-4TQR
   ================================================================
   ```

4. Open the site's **/bank/sorceress** page while signed in and enter the code. The helper notices within a few
   seconds and starts taking your links. If the code runs out first, a new one appears in the logs.

The helper keeps its key in the `sorceress-data` volume. If you remove the helper on the site, it notices, forgets
the key and shows a new pairing code.

### Settings

| Variable         | Default    | Meaning                                                       |
| ---------------- | ---------- | ------------------------------------------------------------- |
| `TASTR_URL`      | (required) | Your Tastr site, `https://` only.                              |
| `SORCERESS_NAME` | host name  | How the helper is listed on the site.                          |
| `SORCERESS_DATA` | `/data`    | Where the key is kept (one file, mode 0600), and the optional config file. |
| `SORCERESS_TMP`  | `/tmp`     | Where audio waits while it's fetched and uploaded.             |
| `SORCERESS_CONFIG` | `<SORCERESS_DATA>/config.toml` | The optional config file ([below](#customising-your-fetcher)). |
| `SORCERESS_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`; wins over the config file. |

The compose file puts `/tmp` on a tmpfs, which lives in memory and is limited to 1.5 GB (a link may be up to 1 GB).
On a device with little memory, mount a folder on disk at `/tmp` instead, for example `- ./sorceress-tmp:/tmp` under
`volumes:` (make it writable for user 10001).

### Commands

`docker compose exec sorceress tastr-sorceress status` shows whether the helper is paired and the site accepts its
key. `tastr-sorceress unpair` deletes the key on the machine; also remove the helper on the site.

## Customising your fetcher

With no config file the helper does what this page describes: it fetches the link with yt-dlp and uploads the
audio as the music site serves it. A config file lets you change that: look somewhere else first (a music library
you already have, or a service on your own network), convert the file, keep a copy, or log more.

The file is TOML. Put it at `/data/config.toml` in the container, or anywhere and point `SORCERESS_CONFIG` at it.
With compose, mount it read-only next to the key:

```yaml
    volumes:
      - sorceress-data:/data
      - ./config.toml:/data/config.toml:ro
      - ./copies:/copies            # only for the copy step; make it writable for user 10001
```

Every key is optional. The helper checks the whole file when it starts and refuses to start on anything it doesn't
know, naming the problem, so a typo can't quietly change what it does. `tastr-sorceress status` shows the sources
and steps it will use.

```toml
# How much to log: debug, info (the default), warning or error. SORCERESS_LOG_LEVEL wins over this.
log_level = "info"

# Where to look for the audio, in order. A source that doesn't find the track hands over to the next;
# the first one that finds it wins. Built in: "yt-dlp". The default is ["yt-dlp"].
sources = ["yt-dlp"]

# What to do with the file once it's found, in order, before it's uploaded. Built in: "convert" and
# "copy". The default is none.
steps = ["convert", "copy"]

# The copy step keeps a copy of every file in this folder, named after the track's title. It never
# writes outside the folder and never overwrites a file (a second copy gets " (2)" added).
[copy]
folder = "/copies"

# The convert step converts the file with ffmpeg, keeping its tags where the new format can hold them.
# format is flac, mp3, opus or m4a. ffmpeg isn't in the default image: see "Plugins and your own image".
[convert]
format = "flac"
# bitrate = "192k"   # for mp3, opus and m4a (defaults 192k, 160k, 192k); flac is lossless

# Hosts on your own network that a plugin may reach, such as a Lidarr or slskd server or a NAS.
# Everything else that resolves to a private or local address stays off limits, and yt-dlp never gets
# an exemption. "name" allows any port; "name:port" or "address:port" only that port.
[network]
local_hosts = ["lidarr.lan", "192.168.1.20:8686"]

# Options for a plugin, handed to it as they are. Only for names in sources or steps.
# [plugins.my-source]
# url = "http://lidarr.lan:8686"
```

A step that fails, or a source that fails in a way other than "not found", fails the job, and the site shows why.

### Following a job

Everything the helper logs while it works on a job is also sent to the site, so the job's page under **Your
fetchers** shows each step: the job taken, what the search found, the format picked, the download at each quarter,
each part uploaded, and why it failed. With `SORCERESS_LOG_LEVEL=debug` (or `--verbose`) the helper logs, and
sends, more: yt-dlp's own output and each file's sha256. Only the job's own lines go; the key is never logged.

### Moments without a link

When you send a moment with no link to your fetchers (a Shazam capture, say), the site gives the helper a search
job instead: the song's title and artist (yours, if you corrected them), any extra words you added such as
"extended mix", and the track's length when it is known. The helper searches YouTube for them, lists every result
in the job's log, and picks the one closest to the track's length. When no result is within 10 seconds (or 5 %)
of it, the job fails rather than saving the wrong version: add words or a link and send it again. Without a known
length it takes the first result with the title in it. The link it picks passes the allowlist like any other.

### What always holds

Whatever sources and steps you use, the helper keeps these rules, so a custom setup can't hurt your account or
your network by mistake:

- The link must pass the allowlist before any source runs; for a search job, that is the link the YouTube search
  picked. A source that searches for the song instead gets the same job, with the title, artist, your extra words
  and length the site knows as hints.
- Before upload the file is checked again: it must be within the site's size and length limits, and a type the
  site takes (webm, m4a, mp4, opus, ogg, mp3, aac, flac or wav).
- Temporary files are deleted after every job. Only the copy step's folder keeps anything.
- Name lookups to private and local addresses are refused, except for the hosts in `local_hosts`.
- Plugins never get the helper's key, and the site is told which source and steps handled each link.

### Plugins and your own image

Other sources and steps come as Python packages that register themselves under the entry point groups
`tastr_sorceress.sources` and `tastr_sorceress.steps`; the helper loads only the ones your config names. The
interface is described in [`tastr_sorceress/plugins.py`](tastr_sorceress/plugins.py). [`examples/plugin`](examples/plugin)
is a small working one: a source that finds the track in a folder of music you already have, and a step that fills
in the title and artist, with a `Dockerfile` that builds your own image from the official one with the plugin and
ffmpeg added. Rebuild your image whenever the official one updates.

A plugin runs inside the helper with the helper's permissions, so install only plugins you have read or trust.
The checks above keep mistakes away from the site and your network; they don't make a hostile plugin safe.

**You are responsible for what your own sources fetch.** This project ships only the yt-dlp source and an example
that reads your own files. It doesn't ship plugins for file-sharing networks or other download services, and
whether using one is legal where you live, and allowed by that service, is yours to check.

## Updating

YouTube changes often, so yt-dlp does too. The image is rebuilt weekly, and on any day a new yt-dlp comes out.
The site marks a helper as out of date when its yt-dlp is more than 30 days old. To update:

```sh
docker compose pull && docker compose up -d
```

Or let a tool such as [Watchtower](https://containrrr.dev/watchtower/) do it for you.

## Checking the image

Use only `ghcr.io/pocketilmatto/tastr-sorceress`, never a copy someone passes around: a changed helper holds your
key and could put any audio in your bank. Every image is built by GitHub Actions from this repository and comes
with a signed build provenance attestation. To check that an image you pulled was built here:

```sh
gh attestation verify oci://ghcr.io/pocketilmatto/tastr-sorceress:latest --repo pocketIlmatto/tastr-sorceress
```

## What it can and can't do

- **One key, one person.** Pairing gives the helper a key that acts only for the account that confirmed it. With it,
  the helper can take that account's queued links, upload their audio and report back. It can't see or change
  anything else on the site, and the site only stores a hash of the key. Removing the helper on the site revokes the
  key at once.
- **An allowlist.** It only fetches `https://` links on youtube.com, youtu.be, music.youtube.com, soundcloud.com,
  bandcamp.com and mixcloud.com (and their subdomains). It checks every link itself before doing anything, even
  though the site checks too, and yt-dlp is limited to the extractors for those sites, so it can't be pointed at any
  other web page. A search job runs yt-dlp's YouTube search (YouTube's extractors only) and fetches the result it
  picks after the same check.
- **Limits.** No playlists, no live streams, nothing longer than the site's limit (3 hours by default) or bigger than
  its size limit (1 GB by default). One link at a time.
- **No open ports.** It only makes outgoing HTTPS connections, to your Tastr site and to the music sites.
- **Nothing kept.** Audio is deleted as soon as it's uploaded or fails; the only file it keeps is its key (unless
  you turn on the copy step).
- **Hardened container.** It runs as a non-root user with a read-only root filesystem, no capabilities and
  `no-new-privileges`. No ffmpeg: audio stays in the format the music site serves (unless you build your own image
  for the convert step).

## Terms

YouTube's terms of service forbid downloading content except through download buttons or links YouTube itself
provides; other sites have their own terms. You run this helper on your own connection for your own account, and
you are responsible for what you fetch with it and for following the terms of the sites you fetch from.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

The tests use a fake site and a fake yt-dlp; they don't touch the network (one convert test runs a real ffmpeg
when there is one). See [AGENTS.md](AGENTS.md) for the rules
for changes.

## Licence

MIT, see [LICENSE](LICENSE). The image also contains third-party software under its own terms; see [NOTICE](NOTICE).
