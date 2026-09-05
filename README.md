# media-placement

A prototype coordinator that makes Deluge downloads follow the physical disk
already assigned to a movie in Radarr or a series in Sonarr. The applications
retain their normal naming and import logic; downloads and library files can
then share real hardlinks without mergerfs.

**Status:** prototype, observation mode by default. Includes an opt-in apply
path and isolated tests; not yet validated against a live media library. Start
with a disposable torrent. This project does not migrate existing media or
choose a new root folder for you.

## Workflow

1. Radarr/Sonarr adds a torrent paused and sends its **On Grab** webhook.
2. The coordinator persists the torrent hash and movie/series ID in SQLite.
3. It verifies the grab against application history and reads the current
   movie/series path through the API.
4. It checks the assigned disk, download directory, capacity, and reservations.
5. In `observe` mode it records a plan and makes no upstream changes.
6. In `apply` mode it disables that torrent's move-on-completion setting,
   asks Deluge to move its storage to that disk's Downloads directory,
   verifies the reported destination, and resumes the torrent once.
7. Radarr/Sonarr's normal completed-download handling imports the file.

There is no custom code to install inside Radarr, Sonarr, or Deluge, and no
Docker socket access. Deluge's Web JSON-RPC API is used; the Web UI must already
be connected to its daemon. PiaPort can continue handling VPN port forwarding
independently.

## Physical layout and prerequisites

Each disk needs a Downloads folder and a separate library folder:

```text
/storage/disk00/
  Downloads/
  Movies/
  TV/
```

Bind the **whole disk** into Radarr, Sonarr, and Deluge at the same path, e.g.
`/mnt/00:/storage/disk00`. The coordinator gets the same mount read-only.
Do not expose Downloads and Movies as separate mounts inside the importing
application: Linux rejects hardlinks across mount boundaries even on one disk.

Assign a pilot movie to `/storage/disk00/Movies/Title` or a pilot series to
`/storage/disk00/TV/Title`. Movie and series paths are authoritative. A series
whose seasons span several disks is outside this prototype's scope. Downloads
and library directories must already exist and be writable by the application
users. Existing root paths through mergerfs are intentionally rejected.

Use the same `/storage/diskNN` paths in all containers; no remote path mapping
is necessary for this layout. The prototype checks the coordinator's mounts
but cannot prove another container's mount identity. Verify the application's
Compose mappings and an actual imported file's underlying inode before trusting
hardlinks. Keep the host's mount ordering and thermal protection.

## Build and configure

Requires Docker Compose and Python 3.11+ for host tests. Runtime Python code
uses only the standard library.

```sh
make check
make image
cp config.example.toml config.toml
mkdir -p secrets
sudo install -d -o 1012 -g 1012 -m 700 state
```

Edit `config.toml` and `compose.yaml` for the actual disks and URLs. The example
assumes the media user is UID/GID 1012; set `PUID`/`PGID` and state ownership to
match your host. The example connects to the existing external `mediaserver`
network and publishes no host ports.

Create these files using a password manager or editor (one value per file):

- `secrets/webhook-token`: a randomly generated token of at least 32 characters.
- `secrets/radarr-api-key`: Radarr Settings → General → Security → API Key.
- `secrets/sonarr-api-key`: Sonarr's API key.
- `secrets/deluge-web-password`: the **Web UI** password, not `/config/auth`
  credentials and not the Gluetun API key.

Give the container's configured UID read access to the secret files and restrict
access to other host users. File-backed Compose secrets preserve host ownership;
root-owned mode-0600 files are not readable by UID 1012. Never commit these files.
If using only one application, remove the other app config and Compose secret.

`check` validates structure without reading secrets or contacting applications.
Starting the service checks that required secret files exist and are nonempty.
HTTP redirects are rejected so credentials are not forwarded to another endpoint.

## Connect a pilot download

Configure a dedicated Deluge download-client entry in each application:

- Name: `Deluge Placement` (must match `download_client` in the config).
- Category/label: `radarr-placement` or `sonarr-placement`.
- Enable **Add Paused**.
- Initially restrict this client to pilot titles using application tags. Ensure
  other download-client entries cannot grab those pilot titles instead.
- Configure Deluge's Label plugin to support these labels; leave automatic
  move-on-completion disabled for them. The coordinator also disables it per
  torrent before resuming, to prevent a later move onto the wrong disk.

In Settings → Connect → Add → Webhook:

| Setting | Radarr | Sonarr |
|---|---|---|
| URL | `http://media-placement:8080/events/radarr` | `http://media-placement:8080/events/sonarr` |
| Method | POST | POST |
| Trigger | On Grab | On Grab |
| Advanced header | `Authorization: Bearer <webhook-token>` | same |

Enter `Authorization` as the header name and `Bearer <webhook-token>` as its
value. The Test button is supported and does not enqueue a download.

Start the coordinator **before grabbing the pilot torrent**. It ignores torrents
and history older than the database's initialization time. The prototype only
accepts paused, unfinished torrents with zero downloaded payload and the matching
dedicated label. Existing downloads and manually added unrelated torrents are
outside its scope. A deliberately manually paused new pilot torrent cannot be
distinguished from an automatically paused one: only use these labels for jobs
the coordinator is authorized to resume.

```sh
export MEDIA_PLACEMENT_VERSION=$(git rev-parse --short=12 HEAD)
docker compose up -d --no-deps media-placement
docker compose logs -f media-placement
docker compose exec media-placement python -m media_placement jobs
```

Observation leaves pilot torrents paused. Inspect the recorded plan. To try
apply mode, change `mode = "apply"`, restart the coordinator, then grab a **new**
disposable torrent. Observed and blocked jobs are terminal and are not silently
replayed when the mode changes. Manually handle an earlier pilot in Deluge if
needed; the prototype has no retry-all or automatic migration command.

## State, recovery, and limits

`GET /jobs` (Bearer authentication) returns the newest 100 jobs and reconciliation
errors. `GET /healthz` is an unauthenticated worker-liveness check, not proof that
the applications are reachable or that hardlinks work. No browser UI is included.

Jobs progress through `pending → moving → resuming → done → complete`, or end as
`observed` / `blocked`. `done` means the resume RPC succeeded, not that the download
or library import finished. `complete` releases the reservation when Deluge reports
the torrent finished or removed; it does not certify a successful library import.

- SQLite commits precede webhook acknowledgement and external mutations.
- Torrent hashes are unique across both applications. Conflicting ownership is
  rejected; duplicate webhooks return the existing job.
- A torrent with more than 100 grab-history records in one application is blocked
  for manual review rather than inferring ownership from an incomplete page.
- API failures retry with backoff, then block after eight failed attempts.
- Recent grab history is reconciled every minute, with an overlapping persisted
  cursor. Each scan reads at most 1,000 records; exceeding that limit retains the
  cursor and exposes an error. No assumption of reliable webhook delivery is made.
- Resume intent is written before the RPC. An ambiguous resume is never blindly
  repeated: recovery confirms it is already running in the correct location or
  blocks for human inspection. A successful resume followed by a manual pause is
  not automatically undone.
- Full download sizes are reserved until completion. Already-written bytes may
  therefore temporarily be counted twice. This is conservative, and only covers
  downloads managed by this coordinator. Other writers can still consume space.
- Blocked jobs that already have a plan keep their reservation. Inspect and resolve
  these manually; there is no automatic disk relocation or reservation expiry.
- Run one replica with one state database; a process lock enforces this on Linux.
- Mount disappearance, path traversal/symlinks, target-name collisions, changed
  library assignments, and insufficient free space stop the operation.

This is not an atomic transaction across three applications. Avoid editing a
pilot torrent or changing its library root during placement. Apply mode can move
Deluge-created empty/preallocated storage, but deliberately refuses downloaded
payload. It never directly deletes media, edits application databases, or changes
a movie/series root folder. The worker's filesystem access to media is read-only.

Back up the SQLite state with the coordinator stopped (including any WAL files),
plus private config and secret files. Keep previous images when updating. An
image rollback is appropriate only while its code supports the existing database
schema; v0.1.0 has no schema migrations.

## Development and deployment

```sh
make test
PYTHONPATH=src python3 -m media_placement --config config.example.toml check
```

The tests use temporary directories, fake application/Deluge APIs, and local HTTP
servers. They never contact your media stack. `ci/github-actions.yml` is an optional
GitHub Actions template that runs checks and builds the image. To enable it, copy
it to `.github/workflows/check.yml` and push with credentials that permit workflow
updates. It does not deploy or publish images. `make image` tags the image with
the current commit; use a clean checkout for releases. Compose uses
`pull_policy: never` for the locally built image.

The mediacenter repository can later own this service's deployment configuration
and pinned image tag. This repository currently includes a standalone Compose
example for the pilot, without modifying an existing media stack.

## API references

- [Radarr webhook payloads](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/Notifications/Webhook/WebhookBase.cs)
- [Sonarr webhook payloads](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/Notifications/Webhook/WebhookBase.cs)
- [Deluge core API](https://deluge.readthedocs.io/en/latest/reference/api.html)
- [Linux hardlink mount restrictions](https://man7.org/linux/man-pages/man2/link.2.html)

MIT licensed. No application source code is bundled.
