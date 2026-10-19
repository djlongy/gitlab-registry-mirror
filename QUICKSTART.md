# Quickstart

`images.txt` is the reviewed image list. `mirror.py` copies it into the low registry (`sync`), packs what is new into a tar (`export`), delivers the tar (`send`) and pushes it into the high registry (`import`).

## By hand

```sh
# Low side, any host with python3 and skopeo
export MIRROR_REGISTRY=registry.low.example.internal
python3 mirror.py login                                   # with MIRROR_REGISTRY_USERNAME and _PASSWORD set
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus
python3 mirror.py sync
python3 mirror.py export -o carry.tar                     # carry.tar and carry.tar.sha256
python3 mirror.py send carry.tar -t /media/usb            # or --nifi-url URL, or --s3-bucket NAME

# High side
export MIRROR_REGISTRY=registry.high.example.internal
python3 mirror.py import /media/usb/carry.tar             # or a received directory, or --s3-bucket NAME
```

1. Add images with `add`, or many at once with `add -T lists/base.txt --prefix team`, and merge `images.txt`.
2. Run `sync` whenever `images.txt` changes. Running it again copies nothing.
3. Run `export`, then `send`. The next `export` takes only what was synced since the last `send`.
   For a quick manual export from a clone, point at the pipeline's ledger instead: `MIRROR_LEDGER=https://<gitlab>/api/v4/projects/<id>/packages/generic/registry-mirror-ledger/v2/ledger.json` and `MIRROR_LEDGER_TOKEN`. Trim `images.txt` to what you want; `export` gives that delta and never changes the ledger.
4. On the high side run `import` on the tar, on the directory the diode writes to, or on the bucket.

## Low pipeline

1. Create a GitLab project from this repository.
2. In **Settings > CI/CD > Variables**, scoped to environment `mirror-low` and masked: `MIRROR_REGISTRY`, `MIRROR_REGISTRY_USERNAME`, `MIRROR_REGISTRY_PASSWORD`.
3. Set how images cross: `MIRROR_REQUEST_URL` (webhook: NiFi pulls them), and/or one tar destination: `MIRROR_SEND_DIRECTORY` (a directory mounted on the runner), `MIRROR_SEND_NIFI_URL`, or `MIRROR_SEND_S3_BUCKET` with `MIRROR_S3_ENDPOINT` and the `AWS_*` keys. Set none to sync only.
4. **Build > Pipelines > Run pipeline** on the default branch. Each run sends what is new. To send a batch again, paste images (or `images.txt` lines) into `MIRROR_RESEND_IMAGES`, one per line; `MIRROR_RESEND_ALL=true` sends everything.

It works when the `sync` job log ends `sent: container-images-<pipeline>.tar` and the high `import` prints `imported: <host>/team/prometheus:v3.13.4@sha256:...`.

`import` moves a tag that points at another digest on the high side, as `docker push` does, and prints `moving tag:` with both digests. Pass `--no-clobber` to refuse the whole tar instead.
