# SOPS runbook

Secrets under `apps/` are committed only as `*.enc.yaml`, encrypted with
[SOPS](https://github.com/getsops/sops) using an age key. Argo CD decrypts them at
sync time with the `sops` Config Management Plugin (a sidecar on
`argocd-repo-server`, image built from `argocd-sops/`).

## One-time setup

```bash
# 1. Local key (back up the private key; losing it makes the secrets unreadable)
mkdir -p ~/.config/sops/age
[ -f ~/.config/sops/age/keys.txt ] || age-keygen -o ~/.config/sops/age/keys.txt   # never overwrite an existing key
grep -q SOPS_AGE_KEY_FILE ~/.zshrc || echo 'export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"' >> ~/.zshrc

# 2. Give the same key to Argo CD (never commit it)
kubectl -n argocd create secret generic sops-age \
  --from-file=keys.txt="$HOME/.config/sops/age/keys.txt"

# 3. Add the sidecar to Argo CD (version pinned in argocd-install/kustomization.yaml)
# 3a. The apply re-applies the WHOLE upstream install (pinned version), not just the sidecar.
#     Confirm the running version matches the pin, back up config, and review the diff first:
kubectl -n argocd get deploy argocd-server -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n argocd get cm argocd-cm argocd-cmd-params-cm argocd-rbac-cm -o yaml > ~/argocd-cm-backup.yaml
kubectl diff -k argocd-install --server-side --force-conflicts   # expect only argocd-repo-server changes
# 3b. Apply
kubectl apply -k argocd-install --server-side --force-conflicts
kubectl -n argocd rollout status deploy/argocd-repo-server
```

The sops-age Secret and the public ghcr.io/pharmlovex/argocd-sops image must exist
before applying; otherwise the new repo-server pod never becomes ready (the old one
keeps serving).

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
`*.enc.yaml` files. If the path contains no YAML files, rendering fails instead of producing an empty app.

## Rotating or adding keys

```bash
# 1. New key, outside the repo
age-keygen -o ~/.config/sops/age/new-keys.txt
age-keygen -y ~/.config/sops/age/new-keys.txt        # public key
# 2. Add that public key to .sops.yaml (comma-separated `age:` recipients), then re-encrypt
sops updatekeys apps/pantry/postgres-secret.enc.yaml
# 3. Give the cluster BOTH identities during the transition (an age key file can hold several)
cat ~/.config/sops/age/keys.txt ~/.config/sops/age/new-keys.txt > /tmp/both-keys.txt
kubectl -n argocd create secret generic sops-age \
  --from-file=keys.txt=/tmp/both-keys.txt --dry-run=client -o yaml | kubectl apply -f -
rm -P /tmp/both-keys.txt
kubectl -n argocd rollout restart deploy/argocd-repo-server
# 4. Commit and push the re-encrypted files, wait for Argo CD to sync
# 5. Only then remove the old recipient from .sops.yaml, run `sops updatekeys` again,
#    push, and replace sops-age with just the new key
```

Note: changing the password in the Secret does not change it inside an existing
Postgres data directory. For that, run `ALTER USER` in the database as well.

## Troubleshooting

| Symptom | Check |
|---|---|
| App shows `ComparisonError ... sops` | `kubectl -n argocd logs deploy/argocd-repo-server -c sops`; usually a wrong or missing key (`failed to get the data key`) |
| `plugin sops not found` / not supported | repo-server pod should be `2/2`: `kubectl -n argocd get pod -l app.kubernetes.io/name=argocd-repo-server` |
| New repo-server pod stuck in `ContainerCreating` (`FailedMount` in events) | Secret `argocd/sops-age` is missing |
| Sidecar crashloops with permission errors | the image must run as uid 999 |
| Pod `ImagePullBackOff` for ghcr.io images | the GHCR package must be public (GitHub → Packages → Package settings → Change visibility) |
| Postgres PVC `Pending` | `storageClassName: local-path` must be set; it is not the default class |
| App shows `no manifests found` | the Application `path` is wrong or the directory has no `*.yaml` files |
| Sidecar still runs old plugin code after an argocd-sops change | `kubectl -n argocd rollout restart deploy/argocd-repo-server` (the sidecar uses `imagePullPolicy: Always`) |

## Caveats

- Rendered manifests (including the decrypted Secret) are cached in argocd-redis; that's inherent to decrypting in a CMP.
- Deleting the pantry Application deletes its namespace, including the Postgres PVC and data (the Application has the resources finalizer and manages namespace.yaml).

## Testing the plugin image locally

```bash
docker build -t argocd-sops:test argocd-sops
IMAGE=argocd-sops:test argocd-sops/test.sh   # uses throwaway age keys, prints PASS
```
