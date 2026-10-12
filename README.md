# gitlab-registry-mirror

Copies reviewed container images and OCI Helm charts into a registry, exports them as tar files, sends the tars across a one-way link and imports them into a registry on the far side, digests unchanged. [Quickstart](QUICKSTART.md).

## Requirements

- `python3` 3.9+ (`import` needs 3.12, or 3.11.4+), `skopeo` 1.13+ and `curl` on the host or runner. No Docker daemon.
- Any OCI registry on either side: Quay, Harbor, GitLab, Artifactory, Nexus or `registry:2`.
- For the low pipeline: a GitLab project from this repository using `.gitlab-ci.yml`.

## Environment

`mirror.py <command> -h` lists every flag. Flags win over these variables.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `TARGET_REGISTRY` | none | This side's registry, `host[:port]`: the low one for `sync`, `export`, `send`; the high one for `import` |
| Optional | `TARGET_REGISTRY_USERNAME`, `TARGET_REGISTRY_PASSWORD` | none | Account for `mirror.py login`; leave unset for a registry without auth |
| Optional | `SOURCE_REGISTRY`, `SOURCE_REGISTRY_USERNAME`, `SOURCE_REGISTRY_PASSWORD` | none | Login for one source registry, e.g. `docker.io` against rate limits. `promote` reads dev from it |
| Optional | `TARGET_REGISTRY_TLS_VERIFY`, `SOURCE_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `MIRROR_PLATFORM` | all platforms | One platform, e.g. `linux/amd64` |
| Optional | `MIRROR_LEDGER` | package registry in CI, else `.mirror-ledger.json` beside `images.txt` | Ledger file, `gitlab`, or the pipeline ledger's `https://` URL, read only, with `PACKAGE_TOKEN` (`read_api`) |
| Optional | `MIRROR_STAGING_DIR` | beside the tar | Disk where `export` and `import` build their working copy |
| Optional | `MIRROR_MAX_SIZE` | one tar | `export --max-size`, e.g. `4GiB` |
| Optional | `MIRROR_REWRITE_BASE_PATH` | none | `--rewrite-base-path` for `export`, `import` and `promote` |
| When S3 | `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | none | Path-style bucket for `send --s3-bucket` and `import --s3-bucket`; `S3_PREFIX`, `S3_REGION`, `S3_CA_BUNDLE`, `S3_TLS_VERIFY` optional |
| Pipeline | `NIFI_URL`, `S3_BUCKET` or `MIRROR_SEND_PATH` | none | Where the low pipeline sends; none set means sync only |
| Pipeline | `EXPORT_ALL` | `false` | Run pipeline with `true` to send every catalog image again |
| Pipeline | `MIRROR_SCAN`, `GRYPE_FAIL_ON`, `MIRROR_JOB_TAG` | `false`, `critical`, any runner | `true` adds the Syft and Grype gate before `sync`; runner tag |

## Usage

```sh
# Low side
export TARGET_REGISTRY=registry.low.example.internal
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus   # pins the digest in images.txt
python3 mirror.py sync                                    # copy images.txt into TARGET_REGISTRY
python3 mirror.py export -o carry.tar                     # what sync added and send has not delivered
python3 mirror.py send carry.tar --path /mnt/transfer     # or --nifi-url URL, or --s3-bucket NAME

# High side
export TARGET_REGISTRY=registry.high.example.internal
python3 mirror.py import /mnt/diode/received              # or --s3-bucket NAME, or one tar
```

`export` also takes images by name: `team/app:v1` (in `TARGET_REGISTRY`), `registry.example.internal/team/app:v1`, `team/app` (every tag) or `--list carry.txt`. `--all` exports the whole catalog. `--rewrite-base-path new-org-dev` lands `team/prom/prometheus` as `new-org-dev/prom/prometheus`; it works on `export` or `import`.

## Preconditions

- Each target registry holds the repositories, or the push account may create them.
- A `--path` destination exists and is mounted; `send` refuses a missing directory.
- In the pipeline: protect the default branch, turn on **Pipelines must succeed**, add a nightly schedule. With `MIRROR_SCAN=true`, the `scan` runner reaches `grype.anchore.io` or `GRYPE_DB_UPDATE_URL`, and a shell runner needs `podman`.
- Quay refuses Windows manifests: set `MIRROR_PLATFORM` for images with Windows children.

## Behaviour

- `sync` is idempotent: a tag already at its digest is left alone, a missing one is copied again. A `latest` image also gets the version tag it reports, unless the catalog pins that tag.
- The ledger records what `sync` put in `TARGET_REGISTRY` and what `send` delivered. `export` with no names takes catalog images synced and not yet sent, so a failed `send` repeats them next time. A missing local ledger starts a new one.
- `export` writes `NAME.tar` and `NAME.tar.sha256`, uncompressed, one `skopeo dir:` copy per image with shared layers stored once. With `--max-size` it writes `NAME-001-of-00N.tar` and so on; an image over the cap goes in a tar of its own.
- `send` checks every tar against its `.sha256` first, then delivers each tar and, last, its `.sha256`. A directory sees `.partial` until a file is complete. NiFi gets an HTTP/1.1 POST with `Filename` and `X-Sha256` headers.
- `import` checks the `.sha256`, refuses to move an existing tag to another digest without `--force`, skips tags already at their digest and verifies each digest after the push. A directory or bucket is an inbox: each tar is removed once imported (`--keep` leaves it), a tar without its `.sha256` waits. A tar named on its own is kept.
- Staging directories (`.mirror-staging-*`) and skopeo's scratch are removed after every run, failed or not.
- `import --record imported.json` lists what it pushed; `promote imported.json` copies those digests from `SOURCE_REGISTRY` (dev) to `TARGET_REGISTRY` (prod).
- With the pipeline ledger's URL, a manual `export` from a clone takes the catalog on disk minus what the pipeline already sent, among images the pipeline synced at the same digest. The ledger is only read, never written.
- `pending` prints catalog sources `sync` has not copied; the pipeline scans exactly those.

## Out of scope

- Signing or approving across the link: the `.sha256` catches corruption, not tampering.
- Configuring NiFi, the diode or the bucket; they move `NAME.tar` and `NAME.tar.sha256` as files.
- Removing old tags from either registry.

## Expected result

`sync` ends `synced N image(s) into <host>`, `send` ends `sent: NAME.tar`, and `import` prints `imported: <host>/team/prometheus:v3.13.4@sha256:...`. Check one:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/prometheus:v3.13.4 | sha256sum
```
