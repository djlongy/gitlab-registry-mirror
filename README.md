# gitlab-registry-mirror

## TL;DR

Copies reviewed images and OCI Helm charts into a low registry and asks the low NiFi, by webhook, to carry them across a one-way link, digests unchanged. By hand, it exports images as tars and imports them on the far side. Pipeline setup: [QUICKSTART.md](QUICKSTART.md).

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

### Bypass the low registry

```sh
# Low side: images.txt lines straight from upstream, at their pinned digest, no MIRROR_REGISTRY or ledger
python3 mirror.py export --from-source -T images.txt -o carry.tar        # chosen lines, any list file
python3 mirror.py export --from-source --all -o catalog.tar              # the whole catalog
python3 mirror.py export --from-source ghcr.io/org/app:1.0 org-dev/org/app -o app.tar   # one line

# High side, as above
python3 mirror.py import /media/usb/carry.tar                            # lands as org-dev/org/app:1.0
```

- Each line is `source [target]`, as in `images.txt`. A source without a target lands at its path without the host. A source without `@sha256:` is exported at the digest its tag has now.

### Mirror the catalog

```sh
# Low side
export MIRROR_REGISTRY=registry.low.example.internal
python3 mirror.py login
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus   # pins the digest in images.txt
# No access to docker.io? Write "docker.io/prom/prometheus:v3.13.4 team/prometheus" and push: Renovate pins it
export MIRROR_REQUEST_URL=https://nifi-low.example.internal/image-transfer-webhook
python3 mirror.py sync              # copy images.txt into MIRROR_REGISTRY, then request what is new from NiFi
```

The low NiFi pulls each requested image from `MIRROR_REGISTRY` and carries it to the high registry. The pipeline runs the same `sync`. A line pushed without its digest gets a Renovate merge request that pins it: setup in [docs/renovate-pinning.md](docs/renovate-pinning.md).

### Send a batch again

Every run requests only what is new. To send chosen images again, open **Build > Pipelines > Run pipeline** and paste them into `MIRROR_RESEND_IMAGES`, one per line: `team/app:v1`, `team/app` (every tag) or `images.txt` lines as they are. `MIRROR_RESEND_ALL=true` sends every synced image. By hand: `python3 mirror.py request -T batch.txt`.

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
| `export IMAGE...` or `export --all` | low | Writes the named images, or every synced catalog image, to `NAME.tar` and `NAME.tar.sha256` | `--all`, `-T FILE` (`images.txt` lines work), `-o NAME.tar` or `-o -`, `--max-size SIZE`, `--staging-dir DIR`, `--rewrite-base-path ORG` or `--strip-components N`, `-n` |
| `request [IMAGE...]` | low | POSTs one webhook request to `MIRROR_REQUEST_URL` naming images in `MIRROR_REGISTRY` for the low NiFi to carry: what is synced and not yet requested, `--all`, or the names given (references or `images.txt` lines) | `--all`, `-T FILE` (`-` is stdin), `-n` |
| `import SOURCE...` | high | Pushes the images in tars, or every tar in a directory, into `MIRROR_REGISTRY` | `-k`, `--no-clobber`, `--no-checksum`, `--staging-dir DIR`, `--rewrite-base-path ORG` or `--strip-components N`, `-n` |

## Environment

A flag wins over its variable.

| Req | Name | Default | Purpose |
|---|---|---|---|
| **Required** | `MIRROR_REGISTRY` (`--registry`) | none | This side's registry, `host[:port]`: the low one for `sync`, `request` and `export`, the high one for `import` |
| Optional | `MIRROR_REGISTRY_USERNAME`, `MIRROR_REGISTRY_PASSWORD` | none | Account `mirror.py login` stores for skopeo. Unset for a registry without auth |
| Optional | `MIRROR_SOURCE_REGISTRY` (`--source-registry`), `MIRROR_SOURCE_REGISTRY_USERNAME`, `MIRROR_SOURCE_REGISTRY_PASSWORD` | none | An upstream registry `sync` copies from, such as `docker.io`, for `login` to store credentials for |
| Optional | `MIRROR_REGISTRY_TLS_VERIFY`, `MIRROR_SOURCE_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `MIRROR_CA_BUNDLE` | system roots | PEM of extra CAs, trusted by every connection on top of the system roots and `certs.d` |
| Optional | `MIRROR_PLATFORM` | all platforms | `sync` copies one platform, e.g. `linux/amd64`. Quay refuses Windows manifests, so set it for images with Windows children |
| Optional | `MIRROR_LEDGER` | package registry in CI, else `.mirror-ledger.json` | Ledger file, or the pipeline ledger's `https://` URL (read only). `MIRROR_LEDGER_TOKEN`, `MIRROR_LEDGER_PROJECT` and `MIRROR_GITLAB_API_URL` reach the CI ledger from outside a job |
| Optional | `MIRROR_RETRY_TIMES` | `7` | How often skopeo retries a failed copy step, 1, 2, 4 ... s apart. The default waits 127 s, past Quay's 503 `UNAVAILABLE` ("The blob is temporarily locked"), which lasts while its garbage collector holds a new blob's lock, up to 120 s |
| Optional | `MIRROR_STAGING_DIR` | `/tmp`, and beside the tar | A large disk for every command's temporary files (skopeo's included) and the `export` and `import` working copy, instead of a small `/tmp`. `--staging-dir` for one `export` or `import` |
| Optional | `MIRROR_EXPORT_MAX_SIZE` | one tar | `export --max-size` |
| Optional | `MIRROR_REWRITE_BASE_PATH`, `MIRROR_STRIP_COMPONENTS` | none | Change the path on `export` or `import`: swap the first segment, or drop the first N (an integer). The NiFi processors keep their own boolean Strip Base Path property |
| Webhook | `MIRROR_REQUEST_URL`, `MIRROR_REQUEST_TOKEN`, `MIRROR_REQUEST_CA_BUNDLE` | none | Where `sync` and `request` post the request for NiFi to pull images. Unset: `sync` only copies. Token sent as `Authorization: Bearer` |
| Resend | `MIRROR_RESEND_IMAGES`, `MIRROR_RESEND_ALL` | none, `false` | Send these images again, or every synced image, as [above](#send-a-batch-again). `MIRROR_RESEND_ALL` also applies to `sync`, `request` and `export` by hand |
| Pipeline | `MIRROR_SCAN`, `GRYPE_FAIL_ON`, `MIRROR_JOB_TAG` | `false`, `critical`, any runner | The Syft and Grype gate before `sync`, its threshold, the runner tag. See [docs/scanning.md](docs/scanning.md) |
| Pipeline | `MIRROR_RESCAN`, `GRYPE_DB_UPDATE_URL`, `GRYPE_DB_CA_CERT` | `false`, `grype.anchore.io`, system roots | Scan every image again and sync nothing, for a nightly schedule. Where Grype gets its database, and the CA it trusts there |
| Pipeline | `DTRACK_URL`, `DTRACK_API_KEY` | unset | With `MIRROR_SCAN`, `sync` also sends each synced image's SBOM to Dependency-Track. See [docs/scanning.md](docs/scanning.md) |
| Pipeline | `MIRROR_RENOVATE_PROJECT`, `MIRROR_RENOVATE_REF` | none, `main` | The Renovate runner project the `pin` job starts for a line without a digest. See [docs/renovate-pinning.md](docs/renovate-pinning.md) |

## Preconditions

- Each `MIRROR_REGISTRY` holds the repositories, or the push account may create them.
- The low NiFi's PlanRegistryTransfer lists every `images.txt` target path in Allowed Repositories.
- A registry outside `MIRROR_REGISTRY` and `MIRROR_SOURCE_REGISTRY` that `export` reads needs your own `skopeo login`.
- In the pipeline: a protected default branch, **Pipelines must succeed**, a nightly schedule. `MIRROR_SCAN=true` needs `grype.anchore.io` through the proxy or a copy at `GRYPE_DB_UPDATE_URL`, and `podman` on a shell runner.

## Behaviour

- `images.txt` holds one reviewed source per line, pinned by digest, and the repository path it lands at on both sides. `add` writes it. A line without a digest is skipped with a warning until Renovate's merge request pins it.
- `sync` is idempotent: a tag already at its digest is left alone. It records each copy in the ledger.
- `export` reads registries and never changes the ledger. It takes named images or `-T` files exactly as named, or every synced catalog image with `--all`; with neither it refuses. Each image is copied whole by digest. `mirror.py export -h` lists the forms.
- An export tar holds `export.json`, naming each image's source, target path, tag and digest, plus one `skopeo dir:` copy per image, with shared layers stored once. Its `.sha256` sits beside it. `export` writes each file under a hidden name and renames it when complete, so a watcher never takes half a file.
- `import` treats a directory as an inbox: each tar is removed once imported, and one that fails stays put. It checks every image before the first push, moves an existing tag as `docker push` does (`--no-clobber` refuses the whole tar before any push), and verifies each digest after the push.
- `--rewrite-base-path` and `--strip-components` change the path on whichever side you pass them. Pass them on one side only.
- With `MIRROR_REQUEST_URL`, `sync` ends with a `request`: one post naming every synced tag the ledger does not yet record as sent at that digest, copied this run or not (everything with `MIRROR_RESEND_ALL`). JSON body `{"registry", "images": [{"repository", "tag", "digest"}], "references"}`, header `X-Artifact-Type: container-images-request`. A failed post is a warning and is not recorded, so the next `sync` sends it. A tag `sync` copies back at a digest already sent (deleted or expired in `MIRROR_REGISTRY`) leaves `sent`, so it is asked for again: the high registry may have lost it too.
- The ledger (`registry-mirror-ledger/v2/ledger.json` in the project's Package Registry in CI) keeps `synced` (what `sync` copied) and `sent`: per tag, the digest last sent to NiFi, when, and the pipeline URL. GitLab keeps older uploads for `MIRROR_LEDGER_KEEP_DAYS` (7), always the newest two. `export` and `import` never read or change it.
- HTTP 202 means NiFi took the request, not that it carries it: the low NiFi's PlanRegistryTransfer refuses a repository missing from its Allowed Repositories with the bulletin `Plan refused: <repo> is not in Allowed Repositories`. List every `images.txt` target path there (`org/*` covers an org).

## Out of scope

- Signing or approving across the link: the `.sha256` catches corruption, not tampering.
- Removing old tags from either registry.
- Configuring the diode or NiFi.

## Expected result

`sync` ends `requested N image tag(s) from <host>` (or `nothing new to request`), `export` ends `wrote carry.tar and carry.tar.sha256`, and `import` prints `imported: <host>/team/prometheus:v3.13.4@sha256:...`. Verify on the high side:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/prometheus:v3.13.4 | sha256sum
```
