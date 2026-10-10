# Continuous deployment to OVHcloud

Goblin production releases are created only by a push to `main`. In normal use,
branch protection makes that push the result of an explicitly authorized pull
request merge.

The `Deploy production` workflow:

1. runs the complete Python suite and validates the deployment files;
2. publishes `ghcr.io/kaesarou/goblin:<git-sha>` and the convenience tag `main`;
3. uploads a seven-day GitHub artifact containing the immutable deployment manifest;
4. copies the release files to the VPS;
5. deploys the SHA tag and waits for the container to become healthy;
6. stops the failed container when startup fails; it does not roll back trading
   code automatically.

The VPS never checks out the repository. Its `.env` and `data/` directory remain
local and are mounted into every successive image.

## One-time VPS preparation

Install Docker Engine with the Compose plugin on the OVHcloud VPS. Create one
dedicated deployment user and directory. The deployment user must be able to run
Docker without `sudo`; membership in the `docker` group is effectively root access,
so the account and its SSH key must be dedicated to this workflow.

```bash
sudo useradd --create-home --shell /bin/bash goblin-deploy
sudo usermod --append --groups docker goblin-deploy
sudo install --directory --owner goblin-deploy --group goblin-deploy /opt/goblin
sudo install --directory --owner goblin-deploy --group goblin-deploy /opt/goblin/data
sudo -u goblin-deploy install --directory --mode 700 /home/goblin-deploy/.ssh
sudo -u goblin-deploy touch /home/goblin-deploy/.ssh/authorized_keys
sudo chmod 600 /home/goblin-deploy/.ssh/authorized_keys
```

Add the public half of the dedicated deployment key to `authorized_keys`. Copy the
runtime configuration once and restrict it to the deployment user:

```bash
sudo -u goblin-deploy nano /opt/goblin/.env
sudo chmod 600 /opt/goblin/.env
```

Keep eToro credentials exclusively in that VPS file. They must never be GitHub
Actions secrets or part of an image.

## GitHub production configuration

Create a GitHub environment named `production` and add these environment secrets:

| Secret | Value |
|---|---|
| `OVH_HOST` | VPS public IP or DNS name |
| `OVH_USER` | `goblin-deploy` |
| `OVH_SSH_PRIVATE_KEY` | private half of the dedicated deployment key |
| `OVH_SSH_KNOWN_HOSTS` | pinned VPS host-key line from `ssh-keyscan`, verified against the VPS console |

Protect `main` against direct pushes and require the `Tests / Python tests` check.
Do not configure an automatic merge. Optionally require manual approval on the
`production` environment if a second deployment confirmation is desired.

## Runtime files and startup failures

The current deployed SHA is recorded in `/opt/goblin/deployment.json`. Application
state and complete run logs remain below `/opt/goblin/data`. Docker console logs are
rotated at 20 MiB with three files.

Compose sends `SIGTERM` and allows up to two minutes for Goblin to finalize the run
before replacing the container. The deploy script always uses the immutable SHA
tag. If the new container does not become healthy within 90 seconds, it disables
restarts and stops that container. Production also performs its existing eToro
close-watcher observation and read-only schema probe before certifying the release.

## Alpaca alongside production

Each push to `alpaca-experimental` triggers `Deploy Alpaca experimental`. After
the full test suite and Compose validation pass, it publishes the commit SHA and
the convenience tag `alpaca-experimental`, then deploys the immutable SHA image.
It never publishes the `main` tag. The production workflow and Compose file are
unchanged.

| Resource | Main / eToro | Alpaca experimental |
|---|---|---|
| VPS directory | `/opt/goblin` | `/opt/goblin-alpaca` |
| Container | `goblin-bot` | `goblin-alpaca` |
| Runtime configuration | `/opt/goblin/.env` | `/opt/goblin-alpaca/.env` |
| SQLite and logs | `/opt/goblin/data` | `/opt/goblin-alpaca/data` |
| Compose file | `docker-compose.production.yml` | `docker-compose.alpaca.yml` |

Alpaca always uses the explicit Compose project `goblin-alpaca`. Its deployment
has its own concurrency group, VPS lock, release directories, deployment manifest
and temporary Docker authentication directory. A simultaneous main deployment
cannot log out its registry session. There is no global Docker cleanup or Compose
`down`/`--remove-orphans` operation in the Alpaca deployment.

The workflow reuses the **existing `production` GitHub environment** solely for
the same four VPS connection secrets. If its deployment branch policy allows
only `main`, also allow `alpaca-experimental` in **Settings → Environments →
production**. Existing approval rules still apply; no rules are bypassed. Broker
credentials remain exclusively in the VPS `.env`, never in GitHub or the image.

### Prepare the VPS once

From a VPS administrator session, create the second directory:

```bash
sudo install -d -m 750 -o goblin-deploy -g goblin-deploy \
  /opt/goblin-alpaca /opt/goblin-alpaca/data
```

Keep `data/` empty on the first Alpaca start. Do not copy the eToro SQLite file,
logs or caches, and do not symlink either directory to production. The container
mounts this dedicated directory at `/app/data`, so keep the supplied `data/...`
paths inside the Alpaca `.env`. Use `BROKER=alpaca_demo`, the **Alpaca paper account**
keys and `ALPACA_DATA_FEED=iex` for the free feed. The deployment rejects other
broker modes and missing/placeholder keys before replacing an existing release.

After completing the local `goblin-alpaca-free.env`, upload it from Linux:

```bash
ssh -i ~/.ssh/goblin_github_actions -o IdentitiesOnly=yes \
  goblin-deploy@54.37.13.221 \
  'umask 077; cat > /opt/goblin-alpaca/.env.next && chmod 600 /opt/goblin-alpaca/.env.next && mv /opt/goblin-alpaca/.env.next /opt/goblin-alpaca/.env' \
  < "$HOME/Téléchargements/goblin-alpaca-free.env"
```

The transfer is encrypted, creates a mode-600 file and replaces `.env` atomically.
It does not restart either container. Repeat it when changing the configuration;
the next deployment recreates the Alpaca container to apply it.

### First deployment and verification

If the first push ran before the directory and `.env` were ready, the deploy job
fails with `Install /opt/goblin-alpaca/.env before deploying Alpaca`. Once configured,
open **Actions → Deploy Alpaca experimental → the latest run → Re-run failed jobs**.
Subsequent pushes deploy automatically. Re-run the latest release only; an older
run deploys its older SHA. Artifacts expire after seven days; after expiry, use a
new push to produce a fresh release.

Compose allows two minutes for graceful shutdown of the preceding Alpaca process.
It then waits up to 180 seconds for account/universe/feed preflight and startup
reconciliation, reading the current container's manifest and start checkpoint.
This health check makes no broker requests and does not require an open market.
It proves startup completed, not continuous WebSocket connectivity or profitability.
On startup failure, only the Alpaca container is stopped, without automatic rollback.
Configuration or image-pull failures before replacement leave the preceding release
running. The successful SHA is recorded in `/opt/goblin-alpaca/deployment.json`.

```bash
ssh -i ~/.ssh/goblin_github_actions -o IdentitiesOnly=yes \
  goblin-deploy@54.37.13.221 \
  'docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"; docker logs --tail 80 goblin-alpaca'
```
