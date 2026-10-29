# Quickstart

`images.txt` is the reviewed image list. The pipeline copies it into the low registry (`sync`) and asks the low NiFi, by webhook, to carry what is new across (`request`). By hand, `export` packs images into a tar and `import` pushes a tar into the high registry.

## Low pipeline

1. Create a GitLab project from this repository.
2. In **Settings > CI/CD > Variables**, scoped to environment `mirror-low` and masked: `MIRROR_REGISTRY`, `MIRROR_REGISTRY_USERNAME`, `MIRROR_REGISTRY_PASSWORD`, and `MIRROR_REQUEST_URL` (the low NiFi's ListenHTTP). Without `MIRROR_REQUEST_URL` the pipeline only syncs.
3. Add images with `python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus`, or many at once with `add -T lists/base.txt --prefix team`, and merge `images.txt`. Or edit `images.txt` in the GitLab web editor, a line without a digest, and Renovate pins it. A Helm chart from a classic repository is the `helm repo add` URL, a slash and the chart name: `https://charts.jetstack.io/cert-manager:v1.15.0 charts/cert-manager`.
4. Each pipeline sends what is synced and not yet sent; the ledger in the Package Registry records each send (digest, time, pipeline). To send a batch again, **Build > Pipelines > Run pipeline** and paste images (or `images.txt` lines) into `MIRROR_RESEND_IMAGES`, one per line; `MIRROR_RESEND_ALL=true` sends everything.
5. The low NiFi's PlanRegistryTransfer must list every target path in Allowed Repositories (`team/*` covers a team), or it refuses the request.

It works when the `sync` job log ends `requested N image tag(s)` and the image appears on the high registry.

## By hand

```sh
# Low side, any host with python3 and skopeo
export MIRROR_REGISTRY=registry.low.example.internal
python3 mirror.py export team/prometheus:v3.13.4 -o carry.tar   # carry.tar and carry.tar.sha256

# High side
export MIRROR_REGISTRY=registry.high.example.internal
python3 mirror.py import /media/usb/carry.tar                   # or a directory of tars
```

`import` moves a tag that points at another digest on the high side, as `docker push` does, and prints `moving tag:` with both digests. Pass `--no-clobber` to refuse the whole tar instead.
