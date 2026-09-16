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
 && sops --version --disable-version-check

# Argo CD requires CMP sidecars to run as uid 999.
# alpine:3.22 already reserves gid 999 for the built-in "ping" group, so
# adduser's default of creating a same-numbered group would collide;
# create the group separately (letting it pick a free gid) instead.
RUN addgroup argocd \
 && adduser -D -u 999 -G argocd -h /home/argocd argocd

COPY --chmod=0755 generate.sh /usr/local/bin/generate.sh
COPY --chown=argocd:argocd plugin.yaml /home/argocd/cmp-server/config/plugin.yaml

USER 999
WORKDIR /home/argocd
# Entrypoint is supplied by the repo-server pod: /var/run/argocd/argocd-cmp-server
