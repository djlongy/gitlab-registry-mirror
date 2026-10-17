# gitlab-registry-mirror

## TL;DR

Copies reviewed images and OCI Helm charts into a registry, carries them across a one-way link as tars, and imports them on the far side, digests unchanged. Pipeline setup: [QUICKSTART.md](QUICKSTART.md).

## Requirements

- `python3` 3.9 or later (`import` needs 3.12, or 3.11.4 or later), `skopeo` 1.13 or later and `curl` on the host or runner. No Docker daemon.
- Any OCI registry on either side: Quay, Harbor, GitLab, Artifactory, Nexus or `registry:2`.
- For the low pipeline: a GitLab project from this repository using `.gitlab-ci.yml`.

## Usage

### Copy one image across by hand

```sh
# Low side: one image from any registry, named in full (a login only if that registry needs one)
python3 mirror.py export docker.io/library/alpine:3.20.3 -o alpine.tar
# or one image already in your low registry, by its path there
MIRROR_REGISTRY=registry.low.example.internal python3 mirror.py export team/app:v1.2.3 -o app.tar

# Carry alpine.tar AND alpine.tar.sha256 together (USB stick, share, diode)

# High side
export MIRROR_REGISTRY=registry.high.example.internal
python3 mirror.py login                                              # only if the registry needs auth
python3 mirror.py import /media/usb/alpine.tar                       # lands as library/alpine:3.20.3
python3 mirror.py import /media/usb/alpine.tar --rewrite-base-path team   # lands as team/alpine:3.20.3
```

- One image is `host/repo:tag` (any registry) or `repo:tag` (in `MIRROR_REGISTRY`). Name Docker Hub images with their `library/` path: `docker.io/library/alpine:3.20.3`, not `alpine`. A repo with no tag exports every tag of it.
- Name several images on one `export` line, or one per line in a file with `-T FILE` (`-` reads stdin), for one tar holding them all.
- The image lands on the high side at its path without the host, under the same tag and digest. `--rewrite-base-path ORG` swaps the first path segment, `--strip-components N` drops the first N.
- `import` of a named tar checks it against the `.sha256`, pushes it and keeps the tar. `--no-checksum` imports a tar whose `.sha256` was left behind.
- Each image is copied whole, every platform its tag holds.
- `-o -` writes the tar to stdout with no `.sha256`; checksum the stream yourself: `export -o - | tee alpine.tar | sha256sum`.

### Mirror the catalog

```sh
# Low side
export MIRROR_REGISTRY=registry.low.example.internal
python3 mirror.py login
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus   # pins the digest in images.txt
python3 mirror.py sync                                    # copy images.txt into MIRROR_REGISTRY
python3 mirror.py export -o carry.tar                     # what sync added and send has not delivered
python3 mirror.py send carry.tar -t /mnt/transfer         # or --nifi-url URL, or --s3-bucket NAME

# High side
export MIRROR_REGISTRY=registry.high.example.internal
python3 mirror.py login
python3 mirror.py import /mnt/diode/received              # every tar there, each removed once imported
```

## Commands

Global flags go before the command: `--catalog FILE` (default `images.txt`), `--ledger FILE|URL`, `--registry HOST`, `--source-registry HOST`, `-v`, `-q` (no progress on stderr) and `--version`. Progress and warnings go to stderr, data (`list`, `pending`, `check`, dry-run plans, `export -o -`) to stdout. Exit codes: 0 done, 1 failed, 2 wrong usage. `-n` is `--dry-run`: it prints what would happen and changes nothing, a ledger included. `mirror.py <command> -h` gives each command's flags in full, with examples. Link `mirror.py` as `registry-mirror` on your `PATH` and help and messages use that name.

| Command | Side | What it does | Main flags |
|---|---|---|---|
| `login [REGISTRY]` | both | Stores credentials for skopeo, as `skopeo login` does. No operand and no flags: `MIRROR_REGISTRY` and `MIRROR_SOURCE_REGISTRY` from their `_USERNAME` and `_PASSWORD` | `-u USER`, `--password-stdin` |
| `add SOURCE TARGET` or `add -T FILE` | low | Resolves `registry/repo:tag` to its digest and adds the line to `images.txt`, one image or every image a list file names | `-T FILE` (repeatable, `-` is stdin), `--prefix PATH` |
| `list` | low | Validates and lists the catalog | none |
| `pending` | low | Lists catalog sources `sync` has not copied yet | none |
| `check FILE...` | low | Fails when a Containerfile or manifest uses an image the catalog lacks; `-` reads stdin | none |
| `sync` | low | Copies every catalog image into `MIRROR_REGISTRY` at its pinned digest | `-n`, `--dry-run` |
| `export [IMAGE...]` | low | Writes images to `NAME.tar` and `NAME.tar.sha256` | `--all`, `-T FILE`, `-o NAME.tar` or `-o -`, `--max-size SIZE`, `--staging-dir DIR`, `--rewrite-base-path ORG` or `--strip-components N`, `-n` |
| `send TAR...` | low | Delivers tars and their `.sha256` files | `-t DIR`, `--nifi-url URL` or `--s3-bucket NAME` (one), `--flowfile`, `-n` |
| `import [SOURCE...]` | high | Pushes the images in tars, a directory of them or `.ffv3` packages into `MIRROR_REGISTRY` | `--s3-bucket NAME`, `-k`, `--no-clobber`, `--no-checksum`, `--record FILE`, `--staging-dir DIR`, `--rewrite-base-path ORG` or `--strip-components N`, `-n` |
| `promote RECORD` | high | Copies the digests `import --record` wrote from `MIRROR_SOURCE_REGISTRY` (dev) to `MIRROR_REGISTRY` (prod) | `--rewrite-base-path ORG` or `--strip-components N` |

## Environment

A flag wins over its variable.

| Req | Name | Default | Purpose |
|---|---|---|---|
| **Required** | `MIRROR_REGISTRY` (`--registry`) | none | This side's registry, `host[:port]`: the low one for `sync`, `export` and `send`, the high one for `import` |
| Optional | `MIRROR_REGISTRY_USERNAME`, `MIRROR_REGISTRY_PASSWORD` | none | Account `mirror.py login` stores for skopeo. Unset for a registry without auth |
| Optional | `MIRROR_SOURCE_REGISTRY` (`--source-registry`), `MIRROR_SOURCE_REGISTRY_USERNAME`, `MIRROR_SOURCE_REGISTRY_PASSWORD` | none | One source registry to log in to, e.g. `docker.io`. `promote` reads dev from it |
| Optional | `MIRROR_CA_BUNDLE` | system roots | PEM of extra CAs, trusted by every connection on top of the system roots and `certs.d` |
| Optional | `MIRROR_REGISTRY_TLS_VERIFY`, `MIRROR_SOURCE_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `MIRROR_PLATFORM` | all platforms | `sync` copies one platform, e.g. `linux/amd64`. Quay refuses Windows manifests, so set it for images with Windows children |
| Optional | `MIRROR_LEDGER` | package registry in CI, else `.mirror-ledger.json` | Ledger file, or the pipeline ledger's `https://` URL (read only). `MIRROR_LEDGER_TOKEN`, `MIRROR_LEDGER_PROJECT` and `MIRROR_GITLAB_API_URL` reach the CI ledger from outside a job |
| Optional | `MIRROR_STAGING_DIR`, `MIRROR_MAX_SIZE` | beside the tar, one tar | `--staging-dir` for `export` and `import`, `--max-size` for `export` |
| Optional | `MIRROR_REWRITE_BASE_PATH`, `MIRROR_STRIP_COMPONENTS` | none | Change the path on `export`, `import` or `promote`: swap the first segment, or drop the first N (an integer). The NiFi processors keep their own boolean Strip Base Path property |
| When S3 | `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | none | Path-style bucket for `send` and `import`. `S3_PREFIX`, `S3_REGION`, `S3_TLS_VERIFY` optional |
| When webhook | `MIRROR_WEBHOOK_URL`, `MIRROR_WEBHOOK_TOKEN`, `MIRROR_WEBHOOK_CA_BUNDLE` | none | Endpoint `sync` posts to after it copied an image. Token sent as `Authorization: Bearer` |
| Pipeline | `NIFI_URL`, `S3_BUCKET` or `MIRROR_SEND_PATH`, `MIRROR_SEND_FLOWFILE` | none | Where the low pipeline sends. None set means sync only |
| Pipeline | `EXPORT_ALL`, `MIRROR_SCAN`, `GRYPE_FAIL_ON`, `MIRROR_JOB_TAG` | `false`, `false`, `critical`, any runner | Resend everything, the Syft and Grype gate before `sync`, the runner tag |

## Preconditions

- Each target registry holds the repositories, or the push account may create them.
- A `send -t` destination exists and is mounted.
- A registry outside `MIRROR_REGISTRY` and `MIRROR_SOURCE_REGISTRY` that `export` reads needs your own `skopeo login`.
- In the pipeline: a protected default branch, **Pipelines must succeed**, a nightly schedule. `MIRROR_SCAN=true` needs `grype.anchore.io` or `GRYPE_DB_UPDATE_URL`, and `podman` on a shell runner.

## Behaviour

- `images.txt` holds one reviewed source per line, pinned by digest, and the repository path it lands at on both sides. `add` writes it.
- `sync` is idempotent: a tag already at its digest is left alone. It records each copy in the ledger.
- `export` reads registries and never changes the ledger. With no names it takes catalog images synced and not yet sent; `--all` takes every synced image; named images or `-T` files are taken exactly as named. Each image is copied whole by digest. `mirror.py export -h` lists the forms.
- An export tar holds `export.json`, naming each image's source, target path, tag and digest, plus one `skopeo dir:` copy per image, with shared layers stored once. Its `.sha256` sits beside it. `export` and `send` write each file under a hidden name and rename it when complete, so a watcher never takes half a file.
- `send` checks every tar against its `.sha256`, delivers the tar then its `.sha256`, and marks the tar's images sent. A tar whose delivery fails is taken again by the next `export`.
- `import` treats a directory or bucket as an inbox: each tar or `.ffv3` is removed once imported, and one that fails stays put. It checks every image before the first push, moves an existing tag as `docker push` does (`--no-clobber` refuses the whole tar before any push), and verifies each digest after the push.
- `--rewrite-base-path` and `--strip-components` change the path on whichever side you pass them. Pass them on one side only.
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
