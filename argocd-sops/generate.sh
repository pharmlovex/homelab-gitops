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
