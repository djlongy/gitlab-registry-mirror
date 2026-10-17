# gitlab-registry-mirror

## TL;DR

Copies reviewed images and OCI Helm charts into a registry, carries them across a one-way link as tars, and imports them on the far side, digests unchanged. Pipeline setup: [QUICKSTART.md](QUICKSTART.md).

## Requirements

- `python3` 3.9 or later (`import` needs 3.12, or 3.11.4 or later), `skopeo` 1.13 or later and `curl` on the host or runner. No Docker daemon.
- Any OCI registry on either side: Quay, Harbor, GitLab, Artifactory, Nexus or `registry:2`.
- For the low pipeline: a GitLab project from this repository using `.gitlab-ci.yml`.

## Flags

Full set: `mirror.py <command> -h`, which lists every flag with its examples. A flag wins over its variable.

| Req | Name | Default | Purpose |
|---|---|---|---|
| **Required** | `TARGET_REGISTRY` | none | This side's registry, `host[:port]`: the low one for `sync`, `export` and `send`, the high one for `import` |
| Optional | `TARGET_REGISTRY_USERNAME`, `TARGET_REGISTRY_PASSWORD` | none | Account `mirror.py login` stores for skopeo. Unset for a registry without auth |
| Optional | `SOURCE_REGISTRY`, `SOURCE_REGISTRY_USERNAME`, `SOURCE_REGISTRY_PASSWORD` | none | One source registry to log in to, e.g. `docker.io`. `promote` reads dev from it |
| Optional | `CA_BUNDLE` | system roots | PEM of extra CAs, trusted by every connection on top of the system roots and `certs.d` |
| Optional | `TARGET_REGISTRY_TLS_VERIFY`, `SOURCE_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `MIRROR_PLATFORM` | all platforms | `sync` copies one platform, e.g. `linux/amd64`. Quay refuses Windows manifests, so set it for images with Windows children |
| Optional | `MIRROR_LEDGER` | package registry in CI, else `.mirror-ledger.json` | Ledger file, or the pipeline ledger's `https://` URL (read only, with `PACKAGE_TOKEN`) |
| Optional | `MIRROR_STAGING_DIR`, `MIRROR_MAX_SIZE` | beside the tar, one tar | `--staging-dir` for `export` and `import`, `--max-size` for `export` |
| Optional | `MIRROR_REWRITE_BASE_PATH`, `MIRROR_STRIP_BASE_PATH` | none, `false` | Change the first path segment on `export`, `import` or `promote` |
| When S3 | `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | none | Path-style bucket for `send` and `import`. `S3_PREFIX`, `S3_REGION`, `S3_TLS_VERIFY` optional |
| When webhook | `MIRROR_WEBHOOK_URL`, `MIRROR_WEBHOOK_TOKEN`, `MIRROR_WEBHOOK_CA_BUNDLE` | none | Endpoint `sync` posts to after it copied an image. Token sent as `Authorization: Bearer` |
| Pipeline | `NIFI_URL`, `S3_BUCKET` or `MIRROR_SEND_PATH`, `MIRROR_SEND_FLOWFILE` | none | Where the low pipeline sends. None set means sync only |
| Pipeline | `EXPORT_ALL`, `MIRROR_SCAN`, `GRYPE_FAIL_ON`, `MIRROR_JOB_TAG` | `false`, `false`, `critical`, any runner | Resend everything, the Syft and Grype gate before `sync`, the runner tag |

## Usage

```sh
# Low side
export TARGET_REGISTRY=registry.low.example.internal
python3 mirror.py login
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus   # pins the digest in images.txt
python3 mirror.py sync
python3 mirror.py export -o carry.tar
python3 mirror.py send carry.tar --path /mnt/transfer

# High side
export TARGET_REGISTRY=registry.high.example.internal
python3 mirror.py login
python3 mirror.py import /mnt/diode/received
```

## Preconditions

- Each target registry holds the repositories, or the push account may create them.
- A `send --path` destination exists and is mounted.
- A registry outside `TARGET_REGISTRY` and `SOURCE_REGISTRY` that `export` reads needs your own `skopeo login`.
- In the pipeline: a protected default branch, **Pipelines must succeed**, a nightly schedule. `MIRROR_SCAN=true` needs `grype.anchore.io` or `GRYPE_DB_UPDATE_URL`, and `podman` on a shell runner.

## Behaviour

- `images.txt` holds one reviewed source per line, pinned by digest, and the repository path it lands at on both sides. `add` and `add-list` write it.
- `sync` is idempotent: a tag already at its digest is left alone. It records each copy in the ledger.
- `export` reads registries and never changes the ledger. With no names it takes catalog images synced and not yet sent; `--all` takes every synced image; named images or `--list` are taken exactly as named. Each image is copied whole by digest. `mirror.py export -h` lists the forms.
- An export tar holds `export.json`, naming each image's source, target path, tag and digest, plus one `skopeo dir:` copy per image, with shared layers stored once. Its `.sha256` sits beside it. `export` and `send` write each file under a hidden name and rename it when complete, so a watcher never takes half a file.
- `send` checks every tar against its `.sha256`, delivers the tar then its `.sha256`, and marks the tar's images sent. A tar whose delivery fails is taken again by the next `export`.
- `import` treats a directory or bucket as an inbox: each tar or `.ffv3` is removed once imported, and one that fails stays put. It checks every image before the first push, moves an existing tag as `docker push` does (`--keep-tags` refuses), and verifies each digest after the push.
- `--rewrite-base-path` and `--strip-base-path` change the path on whichever side you pass them. Pass them on one side only.
- With `MIRROR_WEBHOOK_URL`, `sync` posts once when it copied a tag: JSON body `{"registry", "images": [{"repository", "tag", "digest"}], "references"}`, header `X-Artifact-Type: container-images-request`. A failed post is a warning.

## Out of scope

- Signing or approving across the link: the `.sha256` catches corruption, not tampering.
- Removing old tags from either registry.
- Configuring the diode, the bucket or NiFi.

## Expected result

`sync` ends `synced N image(s) into <host>`, `export` ends `wrote carry.tar and carry.tar.sha256`, `send` ends `sent: carry.tar`, and `import` prints `imported: <host>/team/prometheus:v3.13.4@sha256:...`. Verify on the high side:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/prometheus:v3.13.4 | sha256sum
```
