# SOPS runbook

Secrets under `apps/` are committed only as `*.enc.yaml`, encrypted with
[SOPS](https://github.com/getsops/sops) using an age key. Argo CD decrypts them at
sync time with the `sops` Config Management Plugin (a sidecar on
`argocd-repo-server`, image built from `argocd-sops/`).

## One-time setup

```bash
# 1. Local key (back up the private key; losing it makes the secrets unreadable)
[ -f ~/.config/sops/age/keys.txt ] || age-keygen -o ~/.config/sops/age/keys.txt   # never overwrite an existing key
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
`*.enc.yaml` files. If the path contains no YAML files, rendering fails instead of producing an empty app.

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
| App shows `no manifests found` | the Application `path` is wrong or the directory has no `*.yaml` files |

## Testing the plugin image locally

```bash
docker build -t argocd-sops:test argocd-sops
IMAGE=argocd-sops:test argocd-sops/test.sh   # uses throwaway age keys, prints PASS
```
