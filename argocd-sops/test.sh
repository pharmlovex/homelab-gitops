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
