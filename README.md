# Muse

Muse is a small self-hosted music server for two listeners. It serves a read-only music library
and its catalog database to a web client: listings, search, playlists, streaming with byte
ranges, likes, listening stats, shared listening, letters, a shared pet and daily playlists.
Song lyrics come from LRCLIB and are cached for two weeks.

A queue can also play as one continuous MP3 stream (`/api/run`), the way the iPhone home-screen
app plays: there the next song of a queue often never loads in the background, so songs change
inside the stream instead. Each song fills exactly its catalog length and is read from disk in
chunks; songs that are not MPEG-1 48 kHz stereo are re-encoded with ffmpeg, which
`deploy/provision.sh` installs. A stream ends early, before the first song it cannot play (no
catalog length, file gone, failed re-encoding), and the client starts a new one from there.

A user with `"hls": true` in `users.json` (a prototype, reported as `hls` by `/api/me`) plays
the same queue over HLS instead: `PUT /api/hls/<id>` opens a session over a list of tracks and
a start offset (the client picks the 22 to 64 character `[A-Za-z0-9_-]` id, so it can set the
`src` before the answer arrives; repeating the PUT returns the same session, an id taken by the
other listener is 404). Its playlist (`/api/hls/<id>.m3u8`) is an EVENT playlist of 29.952 s MP3
segments, so the client sets one `src` for the whole listening session and iOS resumes by
segment after a dropped connection; a playlist asked for before the PUT lands waits up to 5 s
for it. `POST /api/hls/<id>/append` adds songs (up to 500 in a session; `end: true` closes it
with an end marker). Segments are cut at frame boundaries from the same frames `/api/run`
builds, each starting with an ID3 timestamp tag; the last 8 songs are kept in memory. The
playlist and segment URLs are capabilities (no cookie needed); sessions untouched for two days
are deleted, and `/api/hls/` requests go to the stream log.

The repository holds two Python packages:

- `packages/server` (`muse`): the Litestar server.
- `packages/agent` (`muse_agent`): the home agent. It publishes the library to object storage,
  asks a local model for the daily playlists and pulls nightly backups.

## Configuration

The server reads the TOML file named by `MUSE_CONFIG` (default `/etc/muse/muse.toml`); any key
can be overridden with a `MUSE_<SECTION>__<KEY>` environment variable. Start from
`deploy/muse.example.toml`.

The agent reads `MUSE_AGENT_CONFIG` or `%APPDATA%/muse/agent.toml`. Start from
`deploy/agent.example.toml`.

Sound vectors and the mood list live in the catalog (`track_vectors`, `vector_space`, `moods`) and
are made on the GPU PC by `scripts/embed_catalog.py` (own PEP 723 environment, not part of the uv
workspace): `uv run scripts/embed_catalog.py --catalog <state.sqlite> --library <lib>` embeds the
tracks that have no vector yet, refits the mean and the vocal-language axis and embeds the moods of
`scripts/moods.json` whose prompt changed; `--import DIR` fills the vectors from the `emb_gemma.npy`
and `ids.json` files in DIR. An optional `[embed] command` in `agent.toml` runs it before every publish.

## Mood engine

Songs are matched by how they sound, not by who listens to them. Every track has a 768-number
vector in the catalog, the moods of `scripts/moods.json` have text vectors in the same space (see
above for the tool that makes them), and the server loads both into memory whenever the catalog
changes (`numpy`). From that come:

- `/api/moods` and `/api/mood/{id}`: about two hundred mood mixes of 200 songs. A mix is drawn
  from the songs nearest to the mood, at most three per artist, neither Russian nor foreign vocals
  above 75 % (a minority side joins only with close matches), one song once however many albums
  carry it, ordered so that neighbours sound alike. The draw is seeded by the mood and the day, so
  both listeners get the same mix and a new one tomorrow; likes and plays play no part.
- `/api/mood-search?q=`: the same draw for a phrase. The phrase is turned into a vector by
  `muse-text`, a small separate service (`deploy/muse-text`, EmbeddingGemma 2 text tower on the
  CPU, `127.0.0.1:8911`, `[moods] text_url` in `muse.toml`). While it is down the search answers
  503 and nothing else changes.
- `/api/radio/{id}?mood=`: radio takes the songs nearest to the seed and to the last songs of the
  queue, skips what was played in the last hours or skipped early in the last month, and leans
  to a mood when one is given. Likes only break ties.
- Genre mixes (200 songs) and the daily playlists (up to 100 songs: the model's picks, then songs
  that sound like them) use the same vectors for what is added.

A catalog without the vector tables still works: the mood endpoints are empty (404 for one mood,
503 for the search) and radio and daily playlists keep the older artist-link behaviour.

## Users and library

Copy `deploy/users.example.json` to `users.json` in the data directory, put in your names and
set passwords with `uv run muse passwd <user-id>`. `animal` and `theme` go to the web client as
they are; `emoji` and `nick` sign the push notifications.

Optionally copy `deploy/dates.example.json` to `dates.json` in the data directory: it feeds the
«Вместе N дней» stamp and the date cards on the home screen, and the song of the day (`pins`)
lives in the user database. Song notes (`kind: "song"` in the per-user files in the `notes`
directory) carry a `track_id` and show the song card; a note whose track left the catalog is
skipped until the track returns.

The catalog is a read-only SQLite file in the schema of `tests/fixtures/catalog_schema.sql`,
with track paths relative to the library. The web client is not part of this repository; the
server serves whatever is built into `web_dir`. User-facing texts are in Russian.

## Development

    uv sync --all-packages --group dev
    uv run pre-commit install
    uv run ruff check . && uv run ruff format --check .
    uv run lint-imports
    uv run ty check
    uv run pytest

Set `MUSE_TEST_REDIS=redis://127.0.0.1:6379/15` to include the Redis cache tests.

    uv run muse db upgrade
    uv run muse serve
    uv run muse passwd <user-id>

## Deployment

`deploy/provision.sh` prepares an Ubuntu host: system users, directories, Redis, the read-only
library mount, systemd units and the firewall. Releases ship from a clean checkout:

    uv run python scripts/deploy.py

The script streams the current commit and the built web client over SSH to
`muse-install-release`. The installer stops the server, saves the user database to
`muse.pre-upgrade.sqlite`, migrates it and starts the new release. The release stays only if the
web client answers and `muse check` finds the user database intact at the expected revision, a
non-empty catalog and a readable track file. Otherwise the saved database goes back and the
previous release starts again.

The library mount keeps directory listings in memory for 720 hours (`--dir-cache-time`) and
takes file times from the bucket's own `LastModified` (`--use-server-modtime`), so a listener's
request never waits for a slow bucket listing or a per-file `HEAD`. Files also change in place
without a new catalog (embedded cover art, repaired lengths), and the mount would keep serving
the old size and bytes of such a file for as long as its listing stays cached, so
`muse-catalog-pull` refreshes the listings through the mount's local remote control
(`127.0.0.1:5572`) before a new catalog goes live and again whenever the last successful refresh
is more than 10 minutes old. That 10-minute stamp is the bound on how long an in-place
replacement stays invisible: do not lengthen it unless the library gets a change trigger of its
own. A refresh runs in the background of the mount and does not block requests. The login is
`RCLONE_RC_USER` and `RCLONE_RC_PASS` in `/etc/muse/storage.env` (see
`deploy/storage.example.env`): rclone reads it from its environment and the pull script hands it
to `curl` on stdin, so the password never appears in a process list. Only `muse-lib` and
`muse-catalog` load that file; the internet-facing `muse` service must not.

## Backups

`muse-backup.timer` packs the user database, `users.json`, `vapid.pem` and the notes into
`/var/backups/muse` every night. The agent pulls each archive, restores it into a scratch
directory and keeps it only if the database passes the integrity check and carries a schema
revision. To bring a server back from an archive:

    systemctl stop muse &&
      rm -f /srv/muse/data/muse.sqlite-wal /srv/muse/data/muse.sqlite-shm &&
      tar -xzf muse-YYYY-MM-DD.tar.gz -C /srv/muse/data --no-overwrite-dir &&
      chown -R muse:muse /srv/muse/data &&
      chmod 600 /srv/muse/data/muse.sqlite &&
      runuser -u muse -- /opt/muse/current/venv/bin/muse db upgrade &&
      runuser -u muse -- /opt/muse/current/venv/bin/muse check &&
      systemctl start muse

Each step runs only after the one before it succeeds, so the server stays stopped when
`muse check` names something the release cannot serve, such as a `users.json` from before a
format change; `deploy/users.example.json` shows the current one.

## License

MIT
