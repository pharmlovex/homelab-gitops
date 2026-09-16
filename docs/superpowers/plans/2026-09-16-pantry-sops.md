# pantry + SOPS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a FastAPI + Postgres app ("pantry") through Argo CD. Its DB credentials are stored in Git encrypted with SOPS/age and decrypted at sync time by a custom Argo CD Config Management Plugin (CMP) sidecar.

**Architecture:** The app manifests live in `apps/pantry/`. Argo CD's Application selects the `sops` CMP, a sidecar on `argocd-repo-server` built from `argocd-sops/`. The sidecar runs `generate.sh`, which prints plain YAML files unchanged and runs `sops --decrypt` on `*.enc.yaml` files using an age key mounted from Secret `argocd/sops-age`. GitHub Actions build both images to GHCR. The Argo CD install is re-applied from a kustomization (`argocd-install/`) that adds the sidecar.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pytest, Postgres 16, SOPS 3.13.3, age, Argo CD CMP v2 sidecar, Kustomize (`kubectl kustomize`), GitHub Actions and GHCR, Tailscale operator Ingress.

**Spec:** `docs/superpowers/specs/2026-09-16-pantry-sops-design.md`

**Environment notes for the executor:**
- Work on branch `gitops-demo-sops` (already checked out).
- Available locally: `sops` 3.13.3, `age`/`age-keygen` 1.3.2, `kubectl` 1.34 (use `kubectl kustomize`; there is no standalone kustomize), `docker` 29, `uv`, `gh`. kubeconform and actionlint are **not** installed; run them through Docker as shown.
- **This machine cannot reach the cluster.** Any `kubectl` command that talks to the cluster must be handed to the user, who runs it and pastes the output back.
- **Never commit a private age key or a `*.dec.yaml` file.**
- Pushing or merging to `main` makes Argo CD and CI act. Ask the user before pushing.

---

## File Structure

| Path | Responsibility |
|---|---|
| `.gitignore` | Block plaintext secrets and Python/venv clutter |
| `.sops.yaml` | SOPS creation rule: which files, which fields, which age recipient |
| `apps/pantry/postgres-secret.enc.yaml` | Encrypted `postgres-credentials` Secret |
| `src/pantry/main.py` | FastAPI app: `/livez`, `/healthz`, `GET/POST /items` |
| `src/pantry/conftest.py`, `src/pantry/test_main.py` | pytest suite (needs a local Postgres) |
| `src/pantry/requirements.txt`, `requirements-dev.txt` | Runtime and test dependencies |
| `src/pantry/Dockerfile`, `.dockerignore` | App image |
| `argocd-sops/generate.sh` | CMP generate logic: cat plain YAML, decrypt `*.enc.yaml` |
| `argocd-sops/plugin.yaml` | `ConfigManagementPlugin` named `sops` |
| `argocd-sops/Dockerfile` | Sidecar image (alpine, sops, age, uid 999) |
| `argocd-sops/test.sh` | End-to-end test of the sidecar image using a throwaway age key |
| `apps/pantry/namespace.yaml`, `postgres.yaml`, `fastapi.yaml`, `service.yaml`, `ingress.yaml` | App manifests |
| `argocd/pantry.yaml` | Argo CD Application using `plugin.name: sops` |
| `argocd-install/kustomization.yaml`, `repo-server-sops-patch.yaml` | Pinned upstream Argo CD install plus sidecar patch |
| `.github/workflows/build-app.yaml`, `build-sops.yaml` | CI: test and build/push images |
| `docs/sops-runbook.md` | Operational runbook |

---

### Task 1: Repo hygiene, age key, `.sops.yaml`

**Files:**
- Create: `.gitignore`
- Create: `.sops.yaml`

- [ ] **Step 1: Create `.gitignore`**

```gitignore
# Plaintext secrets: only *.enc.yaml may be committed
*.dec.yaml
*.dec.yml
keys.txt

# Python
.venv/
__pycache__/
.pytest_cache/
```

- [ ] **Step 2: Ensure a local age key exists (never overwrite one)**

Run:
```bash
KEY="$HOME/.config/sops/age/keys.txt"
if [ -f "$KEY" ]; then echo "key exists"; else mkdir -p "$(dirname "$KEY")" && age-keygen -o "$KEY" && chmod 600 "$KEY"; fi
age-keygen -y "$KEY"
```
Expected: the last line prints the public recipient, starting with `age1`. Keep it for the next step.

Tell the user to add this to `~/.zshrc`, because sops on macOS doesn't look in `~/.config` by default:
```bash
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
```
Every sops command below is prefixed with `SOPS_AGE_KEY_FILE=...` so it works either way.

**Also tell the user to back up this private key somewhere safe, such as a password manager. If it's lost, the secrets in Git can no longer be decrypted.**

- [ ] **Step 3: Create `.sops.yaml` with the real recipient**

Run the following, which substitutes the recipient automatically:
```bash
RECIPIENT="$(age-keygen -y "$HOME/.config/sops/age/keys.txt")"
cat > .sops.yaml <<EOF
creation_rules:
  - path_regex: apps/.*\.enc\.yaml$
    encrypted_regex: ^(data|stringData)$
    age: ${RECIPIENT}
EOF
cat .sops.yaml
```
Expected: the file shows an `age: age1...` line holding a real key.

- [ ] **Step 4: Commit**

```bash
git add .gitignore .sops.yaml
git commit -m "chore: add .sops.yaml (age) and gitignore plaintext secrets"
```

---

### Task 2: Encrypted Postgres secret

**Files:**
- Create: `apps/pantry/postgres-secret.enc.yaml` (from a temporary, git-ignored `apps/pantry/postgres-secret.dec.yaml`)

- [ ] **Step 1: Write the plaintext secret (git-ignored)**

```bash
mkdir -p apps/pantry
PW="$(openssl rand -base64 24 | tr -d '/+=')"
cat > apps/pantry/postgres-secret.dec.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: postgres-credentials
  namespace: pantry
  labels:
    app.kubernetes.io/part-of: pantry
type: Opaque
stringData:
  POSTGRES_USER: pantry
  POSTGRES_PASSWORD: ${PW}
  POSTGRES_DB: pantry
EOF
git status --short apps/pantry
```
Expected: `git status` prints **nothing** for the `.dec.yaml` file, because it's ignored.

- [ ] **Step 2: Encrypt it**

`--filename-override` makes sops match the `.enc.yaml` creation rule:
```bash
SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" \
  sops --encrypt --filename-override apps/pantry/postgres-secret.enc.yaml \
  apps/pantry/postgres-secret.dec.yaml > apps/pantry/postgres-secret.enc.yaml
```

- [ ] **Step 3: Verify the encryption**

```bash
grep -c 'ENC\[AES256_GCM' apps/pantry/postgres-secret.enc.yaml   # expect 3
grep -q "$(grep POSTGRES_PASSWORD apps/pantry/postgres-secret.dec.yaml | awk '{print $2}')" apps/pantry/postgres-secret.enc.yaml && echo "LEAK" || echo "no plaintext"
grep 'name: postgres-credentials' apps/pantry/postgres-secret.enc.yaml
SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt" sops --decrypt apps/pantry/postgres-secret.enc.yaml | diff - apps/pantry/postgres-secret.dec.yaml && echo "round-trip OK"
```
Expected output:
- `3`
- `no plaintext`
- the metadata name line, still readable
- `round-trip OK`

Minor formatting differences in the diff are fine. If there are any, check that all three values match.

- [ ] **Step 4: Delete the plaintext file**

```bash
rm -P apps/pantry/postgres-secret.dec.yaml
```

- [ ] **Step 5: Commit**

```bash
git add apps/pantry/postgres-secret.enc.yaml
git commit -m "feat(pantry): add SOPS-encrypted postgres credentials"
```

---

### Task 3: pantry FastAPI app (TDD)

**Files:**
- Create: `src/pantry/requirements.txt`
- Create: `src/pantry/requirements-dev.txt`
- Create: `src/pantry/conftest.py`
- Create: `src/pantry/test_main.py`
- Create: `src/pantry/main.py`

- [ ] **Step 1: Add dependency files**

`src/pantry/requirements.txt`:
```text
fastapi>=0.115,<1
uvicorn[standard]>=0.30,<1
psycopg[binary]>=3.2,<4
```

`src/pantry/requirements-dev.txt`:
```text
-r requirements.txt
pytest>=8,<10
httpx>=0.27,<1
```

- [ ] **Step 2: Create the venv and start a local Postgres**

```bash
(cd src/pantry && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt)
docker run -d --name pantry-test-pg \
  -e POSTGRES_USER=pantry -e POSTGRES_PASSWORD=pantry -e POSTGRES_DB=pantry \
  -p 55432:5432 postgres:16-alpine
until docker exec pantry-test-pg pg_isready -U pantry -d pantry; do sleep 1; done
```
Expected: this ends with `/var/run/postgresql:5432 - accepting connections`.

- [ ] **Step 3: Write the test fixtures**

`src/pantry/conftest.py`:
```python
import os

import pytest
from fastapi.testclient import TestClient

# Local test Postgres (see plan Task 3 Step 2). Real env vars win.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "55432")
os.environ.setdefault("POSTGRES_USER", "pantry")
os.environ.setdefault("POSTGRES_PASSWORD", "pantry")
os.environ.setdefault("POSTGRES_DB", "pantry")

import main  # noqa: E402


@pytest.fixture
def client():
    with main.get_conn() as conn:
        conn.execute("TRUNCATE items RESTART IDENTITY")
    return TestClient(main.app)


@pytest.fixture
def db_down(monkeypatch):
    # Nothing listens on port 1, so connecting fails immediately
    monkeypatch.setenv("POSTGRES_PORT", "1")
```

- [ ] **Step 4: Write the failing tests**

`src/pantry/test_main.py`:
```python
from fastapi.testclient import TestClient

import main


def test_livez_does_not_need_db(db_down):
    r = TestClient(main.app).get("/livez")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_ok_when_db_up(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_503_when_db_down(db_down):
    r = TestClient(main.app).get("/healthz")
    assert r.status_code == 503


def test_items_empty(client):
    r = client.get("/items")
    assert r.status_code == 200
    assert r.json() == []


def test_create_then_list(client):
    r = client.post("/items", json={"name": "rice"})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "name": "rice"}

    client.post("/items", json={"name": "beans"})
    r = client.get("/items")
    assert r.json() == [{"id": 1, "name": "rice"}, {"id": 2, "name": "beans"}]


def test_create_rejects_empty_name(client):
    r = client.post("/items", json={"name": ""})
    assert r.status_code == 422
```

- [ ] **Step 5: Run the tests and confirm they fail**

Run: `(cd src/pantry && .venv/bin/pytest -q)`
Expected: an error during collection, `ModuleNotFoundError: No module named 'main'`.

- [ ] **Step 6: Implement `main.py`**

`src/pantry/main.py`:
```python
import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id   serial PRIMARY KEY,
    name text   NOT NULL
)
"""

app = FastAPI(title="pantry")

# Table is created on the first successful connection, not at startup,
# so the app starts (and stays live) even when Postgres is down.
_schema_ready = False


def conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    global _schema_ready
    with psycopg.connect(conninfo(), connect_timeout=3) as conn:
        if not _schema_ready:
            conn.execute(SCHEMA)
            conn.commit()
            _schema_ready = True
        yield conn


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class Item(ItemIn):
    id: int


@app.get("/livez")
def livez() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@app.get("/items", response_model=list[Item])
def list_items() -> list[Item]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM items ORDER BY id").fetchall()
    return [Item(id=r[0], name=r[1]) for r in rows]


@app.post("/items", response_model=Item, status_code=201)
def create_item(item: ItemIn) -> Item:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO items (name) VALUES (%s) RETURNING id, name",
            (item.name,),
        ).fetchone()
    return Item(id=row[0], name=row[1])
```

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `(cd src/pantry && .venv/bin/pytest -q)`
Expected: `6 passed`.

- [ ] **Step 8: Commit**

```bash
git add src/pantry/requirements.txt src/pantry/requirements-dev.txt src/pantry/conftest.py src/pantry/test_main.py src/pantry/main.py
git commit -m "feat(pantry): FastAPI items API backed by Postgres"
```
Keep `pantry-test-pg` running for Task 4.

---

### Task 4: pantry Docker image

**Files:**
- Create: `src/pantry/Dockerfile`
- Create: `src/pantry/.dockerignore`

- [ ] **Step 1: Write `.dockerignore`**

```text
.venv/
__pycache__/
.pytest_cache/
conftest.py
test_*.py
requirements-dev.txt
```

- [ ] **Step 2: Write the Dockerfile**

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Build the image and smoke-test it against the local Postgres**

```bash
docker build -t pantry:test src/pantry
docker run -d --rm --name pantry-smoke -p 18000:8000 --read-only \
  -e POSTGRES_HOST=host.docker.internal -e POSTGRES_PORT=55432 \
  -e POSTGRES_USER=pantry -e POSTGRES_PASSWORD=pantry -e POSTGRES_DB=pantry \
  pantry:test
sleep 3
curl -fsS localhost:18000/livez && echo
curl -fsS localhost:18000/healthz && echo
curl -fsS -X POST localhost:18000/items -H 'content-type: application/json' -d '{"name":"smoke"}' && echo
curl -fsS localhost:18000/items && echo
docker stop pantry-smoke
```
Expected:
- `{"status":"ok"}` twice
- then `{"id":N,"name":"smoke"}`
- then a list containing `smoke`

`--read-only` confirms the app works with `readOnlyRootFilesystem`.

- [ ] **Step 4: Clean up the test DB**

```bash
docker rm -f pantry-test-pg
```

- [ ] **Step 5: Commit**

```bash
git add src/pantry/Dockerfile src/pantry/.dockerignore
git commit -m "feat(pantry): add container image"
```

---

### Task 5: `argocd-sops` sidecar image (test first)

**Files:**
- Create: `argocd-sops/test.sh`
- Create: `argocd-sops/generate.sh`
- Create: `argocd-sops/plugin.yaml`
- Create: `argocd-sops/Dockerfile`

- [ ] **Step 1: Write the end-to-end test**

`argocd-sops/test.sh`:
```bash
#!/usr/bin/env bash
# End-to-end test for the argocd-sops image using a throwaway age key.
# Usage: IMAGE=argocd-sops:test argocd-sops/test.sh
set -euo pipefail

IMAGE="${IMAGE:-argocd-sops:test}"
fail() { echo "FAIL: $*" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/app"

age-keygen -o "$work/keys.txt" 2>/dev/null
age-keygen -o "$work/wrong.txt" 2>/dev/null
recipient="$(age-keygen -y "$work/keys.txt")"

cat > "$work/app/configmap.yaml" <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: plain
data:
  key: value
EOF

cat > "$work/secret.yaml" <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: s
stringData:
  password: hunter2
EOF

# Run from $work so the repo's .sops.yaml is not picked up
(cd "$work" && sops --encrypt --age "$recipient" --encrypted-regex '^(data|stringData)$' \
  secret.yaml > app/secret.enc.yaml)
grep -q hunter2 "$work/app/secret.enc.yaml" && fail "plaintext in encrypted file"
chmod -R a+rX "$work"

run() {
  docker run --rm --entrypoint /usr/local/bin/generate.sh \
    -e SOPS_AGE_KEY_FILE=/keys/keys.txt \
    -v "$1:/keys/keys.txt:ro" -v "$work/app:/app:ro" -w /app "$IMAGE"
}

out="$(run "$work/keys.txt")"
grep -q 'password: hunter2' <<<"$out" || fail "secret not decrypted"
grep -q 'name: plain' <<<"$out" || fail "plain manifest missing"
grep -q '^sops:' <<<"$out" && fail "sops metadata leaked into output"
[ "$(grep -c '^kind:' <<<"$out")" = 2 ] || fail "expected 2 documents"

if run "$work/wrong.txt" >/dev/null 2>&1; then
  fail "decryption with wrong key should fail"
fi

[ "$(docker run --rm --entrypoint id "$IMAGE" -u)" = 999 ] || fail "image must run as uid 999"

echo PASS
```

```bash
chmod +x argocd-sops/test.sh
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `IMAGE=argocd-sops:test argocd-sops/test.sh`
Expected: a non-zero exit, because the image doesn't exist yet. Docker reports `Unable to find image 'argocd-sops:test'` or `pull access denied`.

- [ ] **Step 3: Write `generate.sh`**

`argocd-sops/generate.sh`:
```sh
#!/bin/sh
# Argo CD CMP generate: emit every YAML manifest in the app directory,
# decrypting *.enc.yaml with sops. Any failure aborts the whole render.
set -eu

first=1
for f in *.yaml *.yml; do
  [ -e "$f" ] || continue
  [ "$first" -eq 1 ] || printf '\n---\n'
  first=0
  case "$f" in
    *.enc.yaml|*.enc.yml) sops --decrypt "$f" ;;
    *) cat "$f" ;;
  esac
done
```

- [ ] **Step 4: Write `plugin.yaml`**

`argocd-sops/plugin.yaml`. Don't set `spec.version`: doing so renames the plugin to `sops-<version>`.
```yaml
apiVersion: argoproj.io/v1alpha1
kind: ConfigManagementPlugin
metadata:
  name: sops
spec:
  generate:
    command: [/usr/local/bin/generate.sh]
```

- [ ] **Step 5: Write the Dockerfile**

`argocd-sops/Dockerfile`:
```dockerfile
FROM alpine:3.22

ARG SOPS_VERSION=3.13.3
ARG TARGETARCH

RUN apk add --no-cache age ca-certificates \
 && cd /tmp \
 && base="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}" \
 && bin="sops-v${SOPS_VERSION}.linux.${TARGETARCH}" \
 && wget -q "${base}/${bin}" "${base}/sops-v${SOPS_VERSION}.checksums.txt" \
 && grep " ${bin}\$" "sops-v${SOPS_VERSION}.checksums.txt" | sha256sum -c - \
 && install -m 0755 "${bin}" /usr/local/bin/sops \
 && rm -f /tmp/sops-* \
 && sops --version

# Argo CD requires CMP sidecars to run as uid 999
RUN adduser -D -u 999 -h /home/argocd argocd

COPY --chmod=0755 generate.sh /usr/local/bin/generate.sh
COPY --chown=999:999 plugin.yaml /home/argocd/cmp-server/config/plugin.yaml

USER 999
WORKDIR /home/argocd
# Entrypoint is supplied by the repo-server pod: /var/run/argocd/argocd-cmp-server
```

- [ ] **Step 6: Build the image and confirm the test passes**

```bash
docker build -t argocd-sops:test argocd-sops
IMAGE=argocd-sops:test argocd-sops/test.sh
```
Expected: the build log shows `sops 3.13.3`, and the test prints `PASS`.

If `sha256sum -c` fails, the checksum line format changed: inspect it with `wget -qO- https://github.com/getsops/sops/releases/download/v3.13.3/sops-v3.13.3.checksums.txt | head`, then adjust the `grep`.

- [ ] **Step 7: Commit**

```bash
git add argocd-sops/
git commit -m "feat(argocd-sops): CMP sidecar image that decrypts *.enc.yaml"
```

---

### Task 6: pantry Kubernetes manifests

**Files:**
- Create: `apps/pantry/namespace.yaml`
- Create: `apps/pantry/postgres.yaml`
- Create: `apps/pantry/fastapi.yaml`
- Create: `apps/pantry/service.yaml`
- Create: `apps/pantry/ingress.yaml`

- [ ] **Step 1: `namespace.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: pantry
  labels:
    app.kubernetes.io/part-of: pantry
```

- [ ] **Step 2: `postgres.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: pantry
  labels:
    app.kubernetes.io/name: postgres
    app.kubernetes.io/part-of: pantry
spec:
  clusterIP: None
  selector:
    app.kubernetes.io/name: postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: postgres
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: pantry
  labels:
    app.kubernetes.io/name: postgres
    app.kubernetes.io/part-of: pantry
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: postgres
  template:
    metadata:
      labels:
        app.kubernetes.io/name: postgres
        app.kubernetes.io/part-of: pantry
    spec:
      serviceAccountName: default
      containers:
        - name: postgres
          image: postgres:16-alpine
          ports:
            - name: postgres
              containerPort: 5432
          envFrom:
            # Decrypted from postgres-secret.enc.yaml by the Argo CD sops plugin
            - secretRef:
                name: postgres-credentials
          env:
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          readinessProbe:
            exec:
              command: ["sh", "-c", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""]
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["sh", "-c", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""]
            initialDelaySeconds: 30
            periodSeconds: 30
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              memory: 512Mi
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        # local-path is not the cluster default StorageClass
        storageClassName: local-path
        resources:
          requests:
            storage: 1Gi
```

- [ ] **Step 3: `fastapi.yaml`**

Start with the `latest` tag. Task 10 pins it to a `sha-` tag once CI has pushed an image.
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pantry
  namespace: pantry
  labels:
    app.kubernetes.io/name: pantry
    app.kubernetes.io/part-of: pantry
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: pantry
  template:
    metadata:
      labels:
        app.kubernetes.io/name: pantry
        app.kubernetes.io/part-of: pantry
    spec:
      # Explicit: removing serviceAccountName leaves the deprecated
      # serviceAccount field behind on the live object, which re-sets it
      serviceAccountName: default
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: pantry
          image: ghcr.io/pharmlovex/pantry:latest
          ports:
            - name: http
              containerPort: 8000
          envFrom:
            - secretRef:
                name: postgres-credentials
          env:
            - name: POSTGRES_HOST
              value: postgres
            - name: POSTGRES_PORT
              value: "5432"
          livenessProbe:
            httpGet:
              path: /livez
              port: http
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            periodSeconds: 10
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 25m
              memory: 64Mi
            limits:
              memory: 256Mi
```

- [ ] **Step 4: `service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: pantry
  namespace: pantry
  labels:
    app.kubernetes.io/name: pantry
    app.kubernetes.io/part-of: pantry
spec:
  selector:
    app.kubernetes.io/name: pantry
  ports:
    - name: http
      port: 80
      targetPort: http
```

- [ ] **Step 5: `ingress.yaml`**

```yaml
# Exposed on the tailnet by the Tailscale Kubernetes operator
# at https://pantry.<tailnet-name>.ts.net
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: pantry
  namespace: pantry
  labels:
    app.kubernetes.io/name: pantry
    app.kubernetes.io/part-of: pantry
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: pantry
      port:
        name: http
  tls:
    - hosts:
        - pantry
```

- [ ] **Step 6: Validate the plain manifests with kubeconform**

```bash
docker run --rm -v "$PWD:/w" -w /w ghcr.io/yannh/kubeconform:latest -strict -summary \
  apps/pantry/namespace.yaml apps/pantry/postgres.yaml apps/pantry/fastapi.yaml \
  apps/pantry/service.yaml apps/pantry/ingress.yaml
```
Expected: `Summary: 6 resources found in 5 files - Valid: 6, Invalid: 0, Errors: 0, Skipped: 0`.

- [ ] **Step 7: Validate the full plugin output, including the decrypted Secret**

This check runs the real sidecar image against the real directory and your real key:
```bash
tmpkey="$(mktemp)"; cp "$HOME/.config/sops/age/keys.txt" "$tmpkey"; chmod 644 "$tmpkey"
docker run --rm --entrypoint /usr/local/bin/generate.sh \
  -e SOPS_AGE_KEY_FILE=/keys/keys.txt -v "$tmpkey:/keys/keys.txt:ro" \
  -v "$PWD/apps/pantry:/app:ro" -w /app argocd-sops:test > /tmp/pantry-rendered.yaml; rm -P "$tmpkey"
docker run --rm -i ghcr.io/yannh/kubeconform:latest -strict -summary - < /tmp/pantry-rendered.yaml
grep -c '^kind:' /tmp/pantry-rendered.yaml
rm -P /tmp/pantry-rendered.yaml
```
Expected:
- kubeconform reports `Valid: 7, Invalid: 0`
- the `grep -c` count prints `7`

The seven resources are: Namespace, Secret, Service (postgres), StatefulSet, Deployment, Service (pantry), and Ingress.

The rendered file contains the plaintext password. The last command deletes it; don't skip it.

- [ ] **Step 8: Commit**

```bash
git add apps/pantry/namespace.yaml apps/pantry/postgres.yaml apps/pantry/fastapi.yaml apps/pantry/service.yaml apps/pantry/ingress.yaml
git commit -m "feat(pantry): kubernetes manifests (postgres, api, tailscale ingress)"
```

---

### Task 7: Argo CD Application

**Files:**
- Create: `argocd/pantry.yaml`

- [ ] **Step 1: Write the Application**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pantry
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/pharmlovex/homelab-gitops.git
    targetRevision: main
    path: apps/pantry
    # Rendered by the argocd-sops CMP sidecar, which decrypts *.enc.yaml
    plugin:
      name: sops
  destination:
    server: https://kubernetes.default.svc
    namespace: pantry
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

- [ ] **Step 2: Validate it against the Argo CD CRD schema**

```bash
docker run --rm -v "$PWD:/w" -w /w ghcr.io/yannh/kubeconform:latest -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  argocd/pantry.yaml
```
Expected: `Valid: 1, Invalid: 0`.

- [ ] **Step 3: Commit**

```bash
git add argocd/pantry.yaml
git commit -m "feat(argocd): pantry Application using sops plugin"
```

---

### Task 8: Argo CD install kustomization with the sidecar

**Files:**
- Create: `argocd-install/kustomization.yaml`
- Create: `argocd-install/repo-server-sops-patch.yaml`

- [ ] **Step 1: Get the running Argo CD version from the user**

Ask the user to run the following and paste the output:
```bash
kubectl -n argocd get deploy argocd-server -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n argocd get deploy argocd-repo-server -o jsonpath='{.spec.template.spec.volumes[*].name}{"\n"}'
```
The first command prints an image such as `quay.io/argoproj/argocd:v3.1.5`; its tag is `ARGOCD_VERSION`. The second must list `var-files` and `plugins`. Both exist in any Argo CD version from 2.4 onward. **Use the version exactly as the cluster reports it.** A different version would upgrade or downgrade Argo CD when applied.

- [ ] **Step 2: Write `kustomization.yaml`, substituting the version**

```bash
ARGOCD_VERSION=vX.Y.Z   # set to the tag from Step 1
mkdir -p argocd-install
cat > argocd-install/kustomization.yaml <<EOF
# Upstream Argo CD install, pinned to the version running in the cluster,
# plus the sops CMP sidecar on argocd-repo-server.
# Apply: kubectl apply -k argocd-install --server-side --force-conflicts
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: argocd
resources:
  - https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml
patches:
  - path: repo-server-sops-patch.yaml
EOF
cat argocd-install/kustomization.yaml
```
Expected: the `resources` URL contains the real version, for example `/v3.1.5/`.

- [ ] **Step 3: Write the patch**

`argocd-install/repo-server-sops-patch.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: argocd-repo-server
spec:
  template:
    spec:
      containers:
        - name: sops
          image: ghcr.io/pharmlovex/argocd-sops:3.13.3
          command: [/var/run/argocd/argocd-cmp-server]
          env:
            - name: SOPS_AGE_KEY_FILE
              value: /home/argocd/.config/sops/age/keys.txt
          securityContext:
            runAsNonRoot: true
            runAsUser: 999
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: var-files
              mountPath: /var/run/argocd
            - name: plugins
              mountPath: /home/argocd/cmp-server/plugins
            - name: sops-tmp
              mountPath: /tmp
            # Created by hand, never committed (see docs/sops-runbook.md)
            - name: sops-age
              mountPath: /home/argocd/.config/sops/age
              readOnly: true
      volumes:
        - name: sops-tmp
          emptyDir: {}
        - name: sops-age
          secret:
            secretName: sops-age
```

- [ ] **Step 4: Render and inspect the result**

```bash
kubectl kustomize argocd-install > /tmp/argocd-rendered.yaml
python3 - <<'EOF'
import re
docs = open("/tmp/argocd-rendered.yaml").read().split("\n---\n")
repo = [d for d in docs if re.search(r"^kind: Deployment$", d, re.M) and "name: argocd-repo-server\n" in d]
assert len(repo) == 1, len(repo)
d = repo[0]
for needle in ["name: sops\n", "ghcr.io/pharmlovex/argocd-sops:3.13.3", "secretName: sops-age",
               "name: sops-tmp", "argocd-cmp-server", "name: var-files", "name: plugins"]:
    assert needle in d, needle
print("OK: repo-server has sops sidecar")
EOF
grep -c '^kind:' /tmp/argocd-rendered.yaml
rm /tmp/argocd-rendered.yaml
```
Expected: `OK: repo-server has sops sidecar` followed by a resource count of about 50 or more. The exact number depends on the Argo CD version.

- [ ] **Step 5: Commit**

```bash
git add argocd-install/
git commit -m "feat(argocd-install): pinned install with sops CMP sidecar"
```

---

### Task 9: CI workflows

**Files:**
- Create: `.github/workflows/build-app.yaml`
- Create: `.github/workflows/build-sops.yaml`

- [ ] **Step 1: `build-app.yaml`**

```yaml
name: build-app

on:
  push:
    branches: [main]
    paths:
      - src/pantry/**
      - .github/workflows/build-app.yaml
  pull_request:
    paths:
      - src/pantry/**
      - .github/workflows/build-app.yaml
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: pantry
          POSTGRES_PASSWORD: pantry
          POSTGRES_DB: pantry
        ports:
          - 55432:5432
        options: >-
          --health-cmd "pg_isready -U pantry -d pantry"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    defaults:
      run:
        working-directory: src/pantry
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install -r requirements-dev.txt
      - run: pytest -q

  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository_owner }}/pantry
          tags: |
            type=sha,prefix=sha-
            type=raw,value=latest,enable={{is_default_branch}}
      - uses: docker/build-push-action@v6
        with:
          context: src/pantry
          platforms: linux/amd64,linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 2: `build-sops.yaml`**

```yaml
name: build-sops

on:
  push:
    branches: [main]
    paths:
      - argocd-sops/**
      - .github/workflows/build-sops.yaml
  pull_request:
    paths:
      - argocd-sops/**
      - .github/workflows/build-sops.yaml
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - id: version
        run: echo "sops=$(sed -n 's/^ARG SOPS_VERSION=//p' argocd-sops/Dockerfile)" >> "$GITHUB_OUTPUT"
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3

      - name: Build local image for tests
        uses: docker/build-push-action@v6
        with:
          context: argocd-sops
          load: true
          tags: argocd-sops:test
      - name: Install sops and age for test.sh
        run: |
          sudo apt-get update && sudo apt-get install -y age
          curl -fsSLo sops "https://github.com/getsops/sops/releases/download/v${{ steps.version.outputs.sops }}/sops-v${{ steps.version.outputs.sops }}.linux.amd64"
          sudo install -m 0755 sops /usr/local/bin/sops
      - run: IMAGE=argocd-sops:test argocd-sops/test.sh

      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository_owner }}/argocd-sops
          tags: |
            type=raw,value=${{ steps.version.outputs.sops }}
            type=raw,value=latest,enable={{is_default_branch}}
      - uses: docker/build-push-action@v6
        with:
          context: argocd-sops
          platforms: linux/amd64,linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 3: Lint both workflows**

```bash
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest
```
Expected: no output and exit code 0.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/
git commit -m "ci: build and push pantry and argocd-sops images to GHCR"
```

---

### Task 10: Runbook

**Files:**
- Create: `docs/sops-runbook.md`

- [ ] **Step 1: Write the runbook**

````markdown
# SOPS runbook

Secrets under `apps/` are committed only as `*.enc.yaml`, encrypted with
[SOPS](https://github.com/getsops/sops) using an age key. Argo CD decrypts them at
sync time with the `sops` Config Management Plugin (a sidecar on
`argocd-repo-server`, image built from `argocd-sops/`).

## One-time setup

```bash
# 1. Local key (back up the private key; losing it makes the secrets unreadable)
age-keygen -o ~/.config/sops/age/keys.txt
echo 'export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"' >> ~/.zshrc

# 2. Give the same key to Argo CD (never commit it)
kubectl -n argocd create secret generic sops-age \
  --from-file=keys.txt="$HOME/.config/sops/age/keys.txt"

# 3. Add the sidecar to Argo CD (version pinned in argocd-install/kustomization.yaml)
kubectl apply -k argocd-install --server-side --force-conflicts
kubectl -n argocd rollout status deploy/argocd-repo-server
```

## Everyday use

```bash
# Edit an existing secret (decrypts into $EDITOR, re-encrypts on save)
sops apps/pantry/postgres-secret.enc.yaml

# Create a new secret: write it as *.dec.yaml (git-ignored), then
sops --encrypt --filename-override apps/<app>/<name>.enc.yaml \
  apps/<app>/<name>.dec.yaml > apps/<app>/<name>.enc.yaml
rm -P apps/<app>/<name>.dec.yaml

# View without editing
sops --decrypt apps/pantry/postgres-secret.enc.yaml
```

An app uses the plugin by setting `spec.source.plugin.name: sops` in its
Application. The plugin outputs every `*.yaml` file in the path and decrypts
`*.enc.yaml` files.

## Rotating or adding keys

```bash
age-keygen -o new-keys.txt                 # new key
# add its public key to .sops.yaml (comma-separated age recipients), then:
sops updatekeys apps/pantry/postgres-secret.enc.yaml
# update the cluster copy: sops-age must contain a key that can decrypt
kubectl -n argocd create secret generic sops-age \
  --from-file=keys.txt=new-keys.txt --dry-run=client -o yaml | kubectl apply -f -
kubectl -n argocd rollout restart deploy/argocd-repo-server
```

Note: changing the password in the Secret does not change it inside an existing
Postgres data directory. For that, run `ALTER USER` in the database as well.

## Troubleshooting

| Symptom | Check |
|---|---|
| App shows `ComparisonError ... sops` | `kubectl -n argocd logs deploy/argocd-repo-server -c sops`; usually a wrong or missing key (`failed to get the data key`) |
| `plugin sops not found` / not supported | repo-server pod should be `2/2`: `kubectl -n argocd get pod -l app.kubernetes.io/name=argocd-repo-server` |
| Sidecar `CreateContainerConfigError` | Secret `argocd/sops-age` is missing |
| Sidecar crashloops with permission errors | the image must run as uid 999 |
| Pod `ImagePullBackOff` for ghcr.io images | the GHCR package must be public (GitHub → Packages → Package settings → Change visibility) |
| Postgres PVC `Pending` | `storageClassName: local-path` must be set; it is not the default class |
````

- [ ] **Step 2: Commit**

```bash
git add docs/sops-runbook.md
git commit -m "docs: SOPS runbook"
```

---

### Task 11: Rollout (user runs the cluster steps)

Claude cannot reach the cluster. In each step, hand the commands to the user and wait for their output.

- [ ] **Step 1: Ask the user for permission, then merge and push**

Ask the user: "Merge `gitops-demo-sops` into `main` and push? This triggers CI. Argo CD won't deploy pantry until `argocd/pantry.yaml` is applied." Only after they say yes:
```bash
git checkout main
git merge --ff-only gitops-demo-sops
git push origin main
TRIGGER_SHA="sha-$(git rev-parse --short=7 HEAD)"   # tag CI will give the pantry image
echo "$TRIGGER_SHA"
```

- [ ] **Step 2: Watch CI**

```bash
gh run list --limit 5
gh run watch "$(gh run list --workflow build-sops.yaml --limit 1 --json databaseId -q '.[0].databaseId')"
gh run watch "$(gh run list --workflow build-app.yaml --limit 1 --json databaseId -q '.[0].databaseId')"
```
Expected: both workflows succeed. If a run fails, show the log with `gh run view <id> --log-failed` and fix the problem before continuing.

- [ ] **Step 3: User makes both GHCR packages public**

Tell the user to open `https://github.com/users/pharmlovex/packages/container/pantry/settings` and `.../argocd-sops/settings`, then set **Change visibility → Public** on each. Verify:
```bash
curl -fsS -o /dev/null "https://ghcr.io/token?scope=repository:pharmlovex/argocd-sops:pull&service=ghcr.io" && \
  docker manifest inspect ghcr.io/pharmlovex/argocd-sops:3.13.3 >/dev/null && echo sops-ok
docker manifest inspect ghcr.io/pharmlovex/pantry:latest >/dev/null && echo pantry-ok
```
Expected: `sops-ok` and `pantry-ok`. If you're logged in to ghcr.io locally, this only proves the images exist. Public visibility is what matters, and the cluster pull in Step 6 is the real test.

- [ ] **Step 4: Pin the app image to the sha tag and push (ask first)**

`docker/metadata-action` tags the image `sha-<first 7 chars of the pushed HEAD>`, which is `$TRIGGER_SHA` from Step 1. Confirm the tag exists, then pin it. If the shell was restarted, read the tag from the run with `gh run view <build-app run id>`.
```bash
docker manifest inspect "ghcr.io/pharmlovex/pantry:${TRIGGER_SHA}" >/dev/null && echo "tag exists: $TRIGGER_SHA"
sed -i '' "s|ghcr.io/pharmlovex/pantry:latest|ghcr.io/pharmlovex/pantry:${TRIGGER_SHA}|" apps/pantry/fastapi.yaml
grep image: apps/pantry/fastapi.yaml
```
Expected: `tag exists: sha-xxxxxxx`, and the image line now uses that tag.

After asking the user, commit and push:
```bash
git add apps/pantry/fastapi.yaml
git commit -m "chore(pantry): pin image to ${TRIGGER_SHA}"
git push origin main
```

- [ ] **Step 5: User installs the key and the sidecar**

Hand these commands to the user:
```bash
kubectl -n argocd create secret generic sops-age \
  --from-file=keys.txt="$HOME/.config/sops/age/keys.txt"
kubectl apply -k argocd-install --server-side --force-conflicts
kubectl -n argocd rollout status deploy/argocd-repo-server
kubectl -n argocd get pod -l app.kubernetes.io/name=argocd-repo-server
kubectl -n argocd logs deploy/argocd-repo-server -c sops --tail=20
```
Expected:
- the repo-server pod shows `2/2 Running`
- the sidecar log includes `serving on /home/argocd/cmp-server/plugins/sops.sock` or similar

- [ ] **Step 6: User deploys the app**

```bash
kubectl apply -f argocd/pantry.yaml
kubectl -n argocd get application pantry -w     # wait for Synced / Healthy, then Ctrl-C
kubectl -n pantry get secret postgres-credentials
kubectl -n pantry get pods,pvc,ingress
```
Expected:
- the Application shows `Synced` and `Healthy`
- the Secret exists
- both pods are `1/1`
- the PVC is `Bound`
- the Ingress has an address like `pantry.tail165dd5.ts.net`

- [ ] **Step 7: User runs an end-to-end check over the tailnet**

```bash
curl -fsS https://pantry.tail165dd5.ts.net/healthz; echo
curl -fsS -X POST https://pantry.tail165dd5.ts.net/items -H 'content-type: application/json' -d '{"name":"rice"}'; echo
curl -fsS https://pantry.tail165dd5.ts.net/items; echo
```
Expected: `{"status":"ok"}`, then `{"id":1,"name":"rice"}`, then `[{"id":1,"name":"rice"}]`.

If the host comes up as `pantry-1`, an old tailnet device already holds the name. Delete it in the Tailscale admin console and recreate the Ingress.

- [ ] **Step 8: Optional exercise to confirm GitOps drives the secret**

Run `sops apps/pantry/postgres-secret.enc.yaml`, add a key such as `DEMO: hello`, commit, and push. Then check that the key appears in the cluster with `kubectl -n pantry get secret postgres-credentials -o jsonpath='{.data.DEMO}' | base64 -d`.
