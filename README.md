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
| `SORCERESS_DATA` | `/data`    | Where the key is kept (one file, mode 0600). Nothing else.     |
| `SORCERESS_TMP`  | `/tmp`     | Where audio waits while it's fetched and uploaded.             |

The compose file puts `/tmp` on a tmpfs, which lives in memory and is limited to 1.5 GB (a link may be up to 1 GB).
On a device with little memory, mount a folder on disk at `/tmp` instead, for example `- ./sorceress-tmp:/tmp` under
`volumes:` (make it writable for user 10001).

### Commands

`docker compose exec sorceress tastr-sorceress status` shows whether the helper is paired and the site accepts its
key. `tastr-sorceress unpair` deletes the key on the machine; also remove the helper on the site.

## Updating

YouTube changes often, so yt-dlp does too. The image is rebuilt weekly, and on any day a new yt-dlp comes out.
The site marks a helper as out of date when its yt-dlp is more than 30 days old. To update:

```sh
docker compose pull && docker compose up -d
```

Or let a tool such as [Watchtower](https://containrrr.dev/watchtower/) do it for you.

## Checking the image

Every image is built by GitHub Actions from this repository and comes with a signed build provenance attestation.
To check that an image you pulled was built here:

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
  other web page.
- **Limits.** No playlists, no live streams, nothing longer than the site's limit (3 hours by default) or bigger than
  its size limit (1 GB by default). One link at a time.
- **No open ports.** It only makes outgoing HTTPS connections, to your Tastr site and to the music sites.
- **Nothing kept.** Audio is deleted as soon as it's uploaded or fails; the only file it keeps is its key.
- **Hardened container.** It runs as a non-root user with a read-only root filesystem, no capabilities and
  `no-new-privileges`. No ffmpeg: audio stays in the format the music site serves.

## Terms

YouTube's terms of service forbid downloading content except through download buttons or links YouTube itself
provides; other sites have their own terms. You run this helper on your own connection for your own account, and
you are responsible for what you fetch with it and for following the terms of the sites you fetch from.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

The tests use a fake site and a fake yt-dlp; they don't touch the network. See [AGENTS.md](AGENTS.md) for the rules
for changes.

## Licence

MIT, see [LICENSE](LICENSE). The image also contains third-party software under its own terms; see [NOTICE](NOTICE).
