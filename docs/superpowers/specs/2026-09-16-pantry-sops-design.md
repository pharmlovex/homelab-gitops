# pantry: learning SOPS with Argo CD

**Date:** 2026-09-16
**Status:** Approved design

## Goal

Add a small FastAPI + Postgres app to `homelab-gitops` whose database credentials
are stored in Git **encrypted with SOPS (age)** and decrypted by Argo CD at sync
time through a custom Config Management Plugin (CMP) sidecar. The purpose is
learning: every moving part is visible and owned in this repo.

Success = `curl https://pantry.tail165dd5.ts.net/items` returns rows written
by a prior `POST /items`, with the app deployed by Argo CD from a repo containing
only the encrypted secret.

## Decisions

| Topic | Decision |
|---|---|
| Location | Inside `homelab-gitops`, following the `apps/<name>/` + `argocd/<name>.yaml` pattern |
| Decryption | Custom CMP sidecar on `argocd-repo-server` (not KSOPS, not an operator) |
| Key type | age (not PGP) |
| Argo CD install | Existing `kubectl apply -f install.yaml`; patched via a kustomization applied manually |
| Registry | GHCR (`ghcr.io/pharmlovex/...`), packages set public |
| Ingress | Tailscale operator Ingress (`ingressClassName: tailscale`) |
| Storage | `storageClassName: local-path` (not the cluster default) |

## Repository layout

```
.sops.yaml
.gitignore                         # blocks plaintext secret files
.github/workflows/
  build-app.yaml                   # src/pantry/** → ghcr.io/pharmlovex/pantry:<sha>, :latest
  build-sops.yaml                  # argocd-sops/**     → ghcr.io/pharmlovex/argocd-sops:<version>, :latest
src/pantry/
  main.py
  requirements.txt
  Dockerfile
argocd-sops/
  Dockerfile
  plugin.yaml
argocd-install/
  kustomization.yaml               # upstream install.yaml at a pinned Argo CD version
  repo-server-sops-patch.yaml
apps/pantry/
  namespace.yaml
  postgres.yaml                    # headless Service + StatefulSet (volumeClaimTemplate, local-path)
  postgres-secret.enc.yaml         # SOPS-encrypted Secret
  fastapi.yaml                     # Deployment
  service.yaml                     # ClusterIP for FastAPI
  ingress.yaml                     # tailscale Ingress, host "pantry"
argocd/
  pantry.yaml                      # Application, source.plugin.name: sops
```

## Components

### Application (`src/pantry/`)
- FastAPI, served by uvicorn on port 8000, running as a non-root user.
- Endpoints: `GET /healthz` (liveness; also checks the DB with `SELECT 1` for readiness),
  `GET /items` (list), `POST /items` (`{"name": str}` → created row).
- Builds its connection from the env vars `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
  `POSTGRES_HOST` and `POSTGRES_PORT`. The first three come from the Secret; host and port are plain env vars.
- Creates the `items` table (`id serial primary key, name text not null`) on startup when it is missing.
- Uses `psycopg[binary]`; no ORM.

### Secret (`apps/pantry/postgres-secret.enc.yaml`)
- A `v1/Secret` named `postgres-credentials` in namespace `pantry`, with `stringData`
  keys `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`.
- Only `data`/`stringData` values are encrypted (`encrypted_regex: ^(data|stringData)$`), so
  metadata stays readable in diffs.
- The plaintext working file is `*.dec.yaml`, which `.gitignore` excludes.

### `.sops.yaml`
```yaml
creation_rules:
  - path_regex: apps/.*\.enc\.yaml$
    encrypted_regex: ^(data|stringData)$
    age: <age public key>
```

### Postgres (`apps/pantry/postgres.yaml`)
- `postgres:16-alpine` StatefulSet with 1 replica and `envFrom` the Secret.
- A volumeClaimTemplate of 1Gi with `storageClassName: local-path`; `PGDATA` points to a subdirectory.
- Headless Service `postgres` on port 5432.
- A readiness probe runs `pg_isready`.

### FastAPI Deployment and Service
- Image `ghcr.io/pharmlovex/pantry:<pinned sha tag>`, `envFrom` the Secret,
  `POSTGRES_HOST=postgres`, and liveness/readiness probes on `/healthz`.
- Explicit `serviceAccountName: default`, matching the stirling-pdf convention.
- ClusterIP Service `pantry` on port 80, targeting 8000.

### Ingress
- `ingressClassName: tailscale`, default backend `pantry:80`, `tls.hosts: [pantry]`.

### SOPS plugin image (`argocd-sops/`)
- Alpine base with pinned `sops` and `age` binaries (version pinned through build args).
- Runs as uid 999, as Argo CD requires for CMP sidecars.
- The entrypoint is `/var/run/argocd/argocd-cmp-server`, which the repo-server copies in through a shared volume.
- `plugin.yaml` (a `ConfigManagementPlugin`, `metadata.name: sops`) is baked in at
  `/home/argocd/cmp-server/config/plugin.yaml`:
  - `generate`: a shell loop over the `*.yaml` files in the app directory. It runs `sops -d` on files
    ending in `.enc.yaml` and `cat`s the others, printing `---` between documents. It uses `set -eu`,
    so any decryption failure aborts the whole generate step.
  - No `discover` rule. The Application selects the plugin explicitly by name.

### Repo-server patch (`argocd-install/`)
- `kustomization.yaml`: `namespace: argocd` and `resources:` set to the upstream
  `https://raw.githubusercontent.com/argoproj/argo-cd/<pinned version>/manifests/install.yaml`.
  The pinned version must match the running Argo CD version.
- A strategic-merge patch on `Deployment/argocd-repo-server` adds:
  - container `sops` (image `ghcr.io/pharmlovex/argocd-sops:<version>`, `securityContext.runAsNonRoot`,
    `runAsUser: 999`)
  - volume mounts: `var-files` → `/var/run/argocd`, `plugins` → `/home/argocd/cmp-server/plugins`,
    `sops-tmp` (emptyDir) → `/tmp`, and `sops-age` (Secret, read-only) → `/home/argocd/.config/sops/age`
  - env `SOPS_AGE_KEY_FILE=/home/argocd/.config/sops/age/keys.txt`
  - new volumes `sops-tmp` (emptyDir) and `sops-age` (secret `sops-age`)
- The Secret `argocd/sops-age` (key `keys.txt`) is created manually and is **never committed**.

### Argo CD Application (`argocd/pantry.yaml`)
- Repo `https://github.com/pharmlovex/homelab-gitops`, `targetRevision: main`,
  `path: apps/pantry`, `plugin: { name: sops }`.
- Destination namespace `pantry`, automated sync with prune and selfHeal, `CreateNamespace=true`.

### CI
- `build-app.yaml`: runs on pushes to `main` that touch `src/pantry/**`, and on `workflow_dispatch`.
  Needs `permissions: packages: write`, uses `docker/login-action` with `GITHUB_TOKEN`, and runs
  `docker/build-push-action` with tags `sha-<short>` and `latest`.
- `build-sops.yaml`: the same shape for `argocd-sops/**`, with tags `<sops version>` and `latest`.
- Image tags in the manifests are bumped by hand; automating that is out of scope.

## Data flow

1. You edit `postgres-secret.dec.yaml` locally and run
   `sops -e postgres-secret.dec.yaml > postgres-secret.enc.yaml`, then commit only the `.enc` file.
2. Argo CD polls `main`, and the repo-server passes `apps/pantry` to the `sops` sidecar.
3. The sidecar decrypts with the mounted age key and returns plain manifests.
4. Argo CD applies them: the Secret is created, and Postgres and FastAPI start with its values.
5. The app is reachable over the tailnet at `https://pantry.tail165dd5.ts.net`.

## Error handling

- **Missing or wrong key:** `sops` exits non-zero, `generate` fails, and the Application shows
  `ComparisonError` with sops stderr. Nothing is applied.
- **Sidecar not registered:** the Application reports that plugin `sops` was not found. Check that the
  repo-server pod runs 2/2 containers and read the sidecar logs.
- **App can't reach the DB:** readiness on `/healthz` fails, and the pod stays unready without crash-looping.

## Runbook (to include in README or docs)

- One-time key setup: `age-keygen -o ~/.config/sops/age/keys.txt`, then
  `kubectl -n argocd create secret generic sops-age --from-file=keys.txt=$HOME/.config/sops/age/keys.txt`.
- Edit a secret in place: `sops apps/pantry/postgres-secret.enc.yaml`.
- Rotate or add recipients: update `.sops.yaml`, then run `sops updatekeys <file>`.
- Debug: `kubectl -n argocd logs deploy/argocd-repo-server -c sops`.
- Apply the Argo CD patch: `kubectl apply -k argocd-install/`.

## Verification

**Local, run by Claude:**
- `sops -d` round-trips the encrypted file (with a local test key, if the user's key is unavailable).
- `kustomize build argocd-install` renders.
- Manifests validate with `kubectl apply --dry-run=client` or kubeconform.
- `docker build` succeeds for both images.
- The app runs against a local Postgres container, and `/items` round-trips.

**Cluster, run by the user** (Claude's sandbox cannot reach the cluster):
- `kubectl -n argocd get pod -l app.kubernetes.io/name=argocd-repo-server` shows 2/2.
- `argocd app get pantry`, or the UI, shows Synced/Healthy.
- `kubectl -n pantry get secret postgres-credentials` exists.
- `POST /items` followed by `GET /items` over the Tailscale hostname returns the item.

## Out of scope

Helm, CI-driven image tag bumps (Argo CD Image Updater or a commit-back step), multiple environments,
Argo CD managing its own installation, and key management through a cloud KMS.
