# upPollo Stats

Private Unraid dashboard for the `qBittorrent → hardlink → upPollo` upload workflow.

The `main` branch is built automatically with GitHub Actions and published to the private GitHub Container Registry image:

```text
ghcr.io/mlo-tek/uppollo-stats:latest
```

Each build is also tagged with its Git commit SHA.

## Features

- 7 / 30 / 90 day dashboard
- SQLite upload history
- qBittorrent event API
- upPollo API log watcher
- Distinguishes triggered, hardlinked, queued, processing, uploaded, filtered, dupe, skipped and failed states
- Search and status filtering
- Read-only mount for `/var/log/uppollo`
- No Docker Compose required

## Unraid

Use the included `unraid/uppollo-stats.xml` template or change the existing container repository to:

```text
ghcr.io/mlo-tek/uppollo-stats:latest
```

Expected mappings/settings:

```text
WebUI:       8781/tcp
/config:     /mnt/cache/appdata/uppollo-stats
/uppollo-logs (read-only): /mnt/cache/appdata/uppollo/logs
STATS_API_KEY=<your private key>
RETENTION_DAYS=365
TZ=Europe/Berlin
```

Because the image is private, the Unraid Docker daemon must already be logged in to `ghcr.io` with an account/token that can read the package. This is the same setup used by the private `usenet-stats` image.

After that, updates are handled through Unraid's normal **Check for Updates → Update** workflow.

## qBittorrent integration

Copy `scripts/uppollo-qbit-with-stats.sh` to the qBittorrent container as `/config/scripts/uppollo-qbit.sh`, fill in the qBittorrent/upPollo/Stats API keys, and use this completion hook:

```text
/config/scripts/uppollo-qbit.sh "%N" "%F" "%D" "%L" "%G" "%I" "%T"
```

The script sends short, non-blocking event updates to the dashboard. The final upPollo result is derived from upPollo's API logs, so `queued` is not treated as `uploaded`.

## Build

`.github/workflows/docker-publish.yml` builds `linux/amd64` and publishes `latest` after every push to `main`.
