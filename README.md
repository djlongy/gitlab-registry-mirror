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
| Optional | `CA_BUNDLE` | system roots | PEM file of extra CAs or self-signed certificates, e.g. `./certs/ca-bundle.crt`, trusted by every connection in addition to the system roots and `certs.d` |
| Optional | `MIRROR_PLATFORM` | all platforms | One platform, e.g. `linux/amd64` |
| Optional | `MIRROR_LEDGER` | package registry in CI, else `.mirror-ledger.json` beside `images.txt` | Ledger file, `gitlab`, or the pipeline ledger's `https://` URL, read only, with `PACKAGE_TOKEN` (`read_api`) |
| Optional | `MIRROR_STAGING_DIR` | beside the tar | Disk where `export` and `import` build their working copy |
| Optional | `MIRROR_MAX_SIZE` | one tar | `export --max-size`, e.g. `4GiB` |
| Optional | `MIRROR_REWRITE_BASE_PATH`, `MIRROR_STRIP_BASE_PATH` | none, `false` | `--rewrite-base-path` or `--strip-base-path` for `export`, `import` and `promote` |
| When S3 | `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | none | Path-style bucket for `send --s3-bucket` and `import --s3-bucket`; `S3_PREFIX`, `S3_REGION`, `S3_TLS_VERIFY` optional |
| Pipeline | `NIFI_URL`, `S3_BUCKET` or `MIRROR_SEND_PATH` | none | Where the low pipeline sends; none set means sync only. `MIRROR_SEND_FLOWFILE=true` sends one `.ffv3` to S3 or the directory |
| Optional | `MIRROR_WEBHOOK_URL`, `MIRROR_WEBHOOK_TOKEN`, `MIRROR_WEBHOOK_CA_BUNDLE` | none | Endpoint `sync` posts to once when it copied an image, e.g. the low NiFi's `NIFI_URL`; the token is sent as `Authorization: Bearer`; the PEM file is trusted for it in place of `CA_BUNDLE`, on top of the system roots and `SSL_CERT_FILE` |
| Pipeline | `EXPORT_ALL` | `false` | Run pipeline with `true` to send every catalog image again |
| Pipeline | `MIRROR_SCAN`, `GRYPE_FAIL_ON`, `MIRROR_JOB_TAG` | `false`, `critical`, any runner | `true` adds the Syft and Grype gate before `sync`; runner tag |

## How it fits together

```text
low side                                                     high side
images.txt --add--> sync --> TARGET_REGISTRY (low)
                              |
                            export --> NAME.tar + NAME.tar.sha256
                                         |
                                       send --> NiFi / S3 / directory ==diode==> import --> TARGET_REGISTRY (high)
```

On each side `TARGET_REGISTRY` names that side's registry. The ledger (`.mirror-ledger.json`, or the package registry in CI) remembers what `sync` copied and what `send` delivered, so a plain `export` carries only what is new.

## Quick run

```sh
# Low side
export TARGET_REGISTRY=registry.low.example.internal
python3 mirror.py login                                   # with TARGET_REGISTRY_USERNAME and _PASSWORD set
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus   # pins the digest in images.txt
python3 mirror.py sync                                    # copy images.txt into TARGET_REGISTRY
python3 mirror.py export -o carry.tar                     # what sync added and send has not delivered
python3 mirror.py send carry.tar --path /mnt/transfer     # or --nifi-url URL, or --s3-bucket NAME

# High side
export TARGET_REGISTRY=registry.high.example.internal
python3 mirror.py login
python3 mirror.py import /mnt/diode/received              # or --s3-bucket NAME, or one tar
```

## Commands

`mirror.py <command> -h` prints each command's flags and examples. Global flags go before the command: `--catalog FILE` (default `images.txt`), `--ledger FILE|gitlab|URL` (or `MIRROR_LEDGER`) and `-v` (print every skopeo and curl command, or `MIRROR_VERBOSE=true`).

### Credentials and trust (every command)

- skopeo reads credentials from its default auth file (`${XDG_RUNTIME_DIR}/containers/auth.json`, or `REGISTRY_AUTH_FILE`). `mirror.py login` writes it for `TARGET_REGISTRY`, and for `SOURCE_REGISTRY` when `SOURCE_REGISTRY_USERNAME` is set. A registry without authentication needs no login: leave its `_USERNAME` unset.
- Any other registry a command reads, such as `export registry.other.example/team/app:v1`, uses whatever `skopeo login` or `podman login` already stored for that host.
- `CA_BUNDLE` adds CAs to every connection on top of the system roots and `certs.d`. `TARGET_REGISTRY_TLS_VERIFY=false` and `SOURCE_REGISTRY_TLS_VERIFY=false` apply only to that registry, for a plain-HTTP lab registry.

### Catalog: `add`, `add-list`, `targets`, `pending`, `covers`

`images.txt` holds one reviewed image per line: the source with its pinned digest, then the repository path it lands at in `TARGET_REGISTRY`, on both sides. `#` starts a comment.

```text
docker.io/prom/prometheus:v3.13.4@sha256:<digest> team/prometheus
```

| Command | What it does |
|---|---|
| `add SOURCE TARGET` | Resolves `SOURCE` (`registry/repo:tag` in full, e.g. `docker.io/library/alpine:3.20`) to its digest and writes the line. `TARGET` is any depth, e.g. `team/prometheus`. OCI Helm charts work the same way |
| `add-list FOLDER --prefix team` | Adds every image named in `FOLDER/*.txt`, one per line, read as `docker pull` reads them (no host means Docker Hub, no tag means `latest`). `docker.io/prom/prometheus:v3.13.4` lands at `team/prom/prometheus`. Keeps `images.txt` sorted |
| `targets` | Validates the catalog and lists it. Every bad line is reported with its line number |
| `pending` | Prints catalog sources `sync` has not copied yet, one per line. The pipeline scans exactly those |
| `covers FILE...` | Fails when a Containerfile or Kubernetes manifest uses an image the catalog lacks. `helm template chart/ \| mirror.py covers -` reads stdin |

### `sync`

Copies every catalog image into `TARGET_REGISTRY` at its pinned digest and records it in the ledger.

- Idempotent: a tag already at its digest is left alone, a missing one is copied again. Run it whenever `images.txt` changes.
- `MIRROR_PLATFORM=linux/amd64` copies one platform instead of the whole index. Quay refuses Windows manifests, so set it for images with Windows children.
- A `latest` image also gets the version tag it reports, unless the catalog pins that tag.
- `SOURCE_REGISTRY` with its credentials logs in to one source registry, e.g. `docker.io` against rate limits.
- With `MIRROR_WEBHOOK_URL`, `sync` ends with one POST when it copied a tag (see [Webhook](#webhook)).
- Output: `synced N image(s) into <host>`.

### `export`

Copies images out of a registry into one uncompressed tar plus its `.sha256`. It only reads registries and never changes the ledger.

**Which images it takes**

| Form | Takes |
|---|---|
| `export` | Catalog images `sync` put in `TARGET_REGISTRY` that `send` has not delivered at that digest. A catalog image not synced yet is listed as `not synced yet` and skipped |
| `export --all` | Every catalog image `sync` has put in `TARGET_REGISTRY`, delivered or not |
| `export team/app:v1` | That tag in `TARGET_REGISTRY` |
| `export team/app` | Every tag of `team/app` in `TARGET_REGISTRY` |
| `export registry.other.example/team/app:v1` | That tag in another registry, read with skopeo's stored login for that host |
| `export --list carry.txt` | Every reference in the file, one per line, `#` comments, in any of the forms above. Combines with names on the command line |

Named references (on the command line or in `--list`) are exported exactly as named: the catalog and the ledger are not consulted. Each tag is resolved to its digest once and copied whole by digest, every platform the registry holds, so the bytes and digests in the tar match the registry.

**Where it writes**

- `-o carry.tar` writes `carry.tar` and `carry.tar.sha256`. The name must end in `.tar`. Without `-o` the tar is `container-images-<UTC time>.tar` in the current directory. An existing tar or `.sha256` of that name is refused, never overwritten.
- `--max-size 4GiB` (or `MIRROR_MAX_SIZE`) splits the images, in order, into `carry-001-of-003.tar` and so on, each with its `.sha256`. The cap counts the compressed sizes the manifests declare, so a tar can be a few KB over it. An image larger than the cap goes in a tar of its own.
- The working copy is built in a hidden `.mirror-staging-*` directory beside the tar, or in `--staging-dir` (or `MIRROR_STAGING_DIR`) on a bigger disk, and is always removed. Allow about twice the export's size there. skopeo's scratch (`TMPDIR`) goes there too, never `/tmp`.
- The tar and then its `.sha256` are written under hidden names and renamed when complete, so a watcher on the output directory never takes half a file.

**Changing the repository path**

- `--rewrite-base-path new-org-dev` (or `MIRROR_REWRITE_BASE_PATH`) replaces the first path segment: `team/prom/prometheus` lands as `new-org-dev/prom/prometheus`.
- `--strip-base-path` (or `MIRROR_STRIP_BASE_PATH=true`) removes it: `team/prom/prometheus` lands as `prom/prometheus`.
- On `export` the new path is written into the tar. On `import` it is applied to the path in the tar. Use it on one side, not both, or the change applies twice. An empty value changes nothing.

**What is in the tar**

```text
./export.json        {"created": ..., "images": [{"dir": "000001", "source": "<host>/<repo>:<tag>", "target": "<repo>", "tag": ..., "digest": "sha256:..."}]}
./000001/            skopeo dir: copy of the first image (manifest.json, config and layer blobs)
./000002/            ...
```

A layer shared by several images is stored once (hard links). `import` reads only this layout.

**Check it**

```sh
sha256sum -c carry.tar.sha256                                 # the tar is intact
tar -xOf carry.tar ./export.json | python3 -m json.tool       # what it holds and where each image lands
```

Output: one `exported: <host>/<repo>:<tag>@sha256:...` line per image (`as <path>` when rebased), then `wrote carry.tar and carry.tar.sha256 (N image tag(s), BYTES bytes)`. `nothing new to export` means every synced image was already sent: use `--all` or name the images.

**Examples**

```sh
python3 mirror.py export                                       # the delta since the last send
python3 mirror.py export --all -o /share/full.tar              # everything synced
python3 mirror.py export team/prometheus:v3.13.4 team/grafana  # one tag, and every tag of grafana
python3 mirror.py export --list carry.txt --max-size 4GiB      # a list, in tars of 4 GiB
python3 mirror.py export --staging-dir /data/tmp -o /data/out/carry.tar
python3 mirror.py export team/app:v1 --rewrite-base-path new-org-dev
```

For a manual export from a clone without a local ledger, point `MIRROR_LEDGER` at the pipeline's ledger (`https://<gitlab>/api/v4/projects/<id>/packages/generic/registry-mirror-ledger/v2/ledger.json`) with `PACKAGE_TOKEN` (`read_api`). A plain `export` then takes the catalog on disk minus what the pipeline already sent, among images the pipeline synced at the same digest. That ledger is only read.

### `send`

Delivers tars `export` wrote. It checks every tar against its `.sha256` first, then delivers each tar and, last, its `.sha256`.

| Destination | Flag | Notes |
|---|---|---|
| Directory | `--path /mnt/transfer` | Must exist. Each file is written as hidden `.NAME.partial` and renamed when complete, so NiFi ListFile or GetFile with its default filter never takes half a file |
| NiFi | `--nifi-url https://nifi.low:9443/contentListener` | One HTTP/1.1 POST per file to a ListenHTTP |
| S3 | `--s3-bucket transfer` | Path-style bucket at `S3_ENDPOINT` with the `AWS_*` keys; `S3_PREFIX`, `S3_REGION`, `S3_TLS_VERIFY` optional |
| FlowFile | `--flowfile` with `--path` or `--s3-bucket` (or `MIRROR_SEND_FLOWFILE=true`) | One NiFi FlowFile v3 `NAME.tar.ffv3` per tar, holding the tar with its attributes and no separate `.sha256`, for a link that splits files or delivers them out of order |

Every file carries `Filename`, `Content-Type`, `X-Sha256`, `X-Artifact-Type: container-images` and `X-Artifact-Format: tar`: as POST headers, as S3 object metadata (names lowercased by S3), or as FlowFile attributes. In CI it also carries `X-CI-Project`, `X-CI-Pipeline-Url` and `X-CI-Commit-Sha`. NiFi UnpackContent (`flowfile-stream-v3`) gives the same attributes as ListenHTTP; PackageFlowFile repacks it for the next hop.

After a delivery `send` records the tar's images as sent in the ledger of `TARGET_REGISTRY`, so the next plain `export` skips them. A tar whose delivery fails is not recorded, so the next `export` takes its images again. Output: `sent: NAME.tar`.

### `import`

Pushes the images in export tars into `TARGET_REGISTRY` (the high registry), tags and digests unchanged.

| Form | Takes |
|---|---|
| `import /media/usb/carry.tar` | One tar, checked against `carry.tar.sha256` beside it, then kept |
| `import /mnt/diode/received` | Every tar and `.ffv3` in the directory, as an inbox |
| `import --s3-bucket received` | Every tar and `.ffv3` in the bucket, as an inbox |
| `import carry.tar.ffv3` | The tar inside a FlowFile v3 package, checked against its `X-Sha256` attribute |

- **Inbox:** each file is removed once imported (`--keep` leaves it). One that fails stays put and does not hold up the rest. A tar whose `.sha256` has not arrived yet waits for the next run.
- **Checks:** the `.sha256` first (`--no-checksum` imports a tar whose `.sha256` was left behind, on its image digests alone), then every image against the digest in `export.json` before the first push, so a refusal pushes nothing, then each digest again after the push.
- **Tags:** a tag already at its digest is skipped (`already there:`). A tag at another digest moves, as `docker push` moves it, with `moving tag:` and both digests. `--keep-tags` refuses instead and pushes nothing.
- **Paths:** `--rewrite-base-path` and `--strip-base-path` apply to the path recorded in the tar (see `export`).
- **Disk:** unpacking needs up to twice the tar's size, beside the tar or in `--staging-dir` (or `MIRROR_STAGING_DIR`). Needs Python 3.12, or 3.11.4 or later, for safe tar extraction.
- `--record imported.json` appends each pushed tag and digest, for `promote`.
- Output: `imported: <host>/<repo>:<tag>@sha256:...` per image, then `NAME.tar: N image tag(s) in <host>, digests verified`.

### `promote`

`promote imported.json` copies the digests `import --record` wrote from `SOURCE_REGISTRY` (dev) to `TARGET_REGISTRY` (prod), with the same path flags as `import`.

### Webhook

With `MIRROR_WEBHOOK_URL`, `sync` ends with one POST (30 s limit, two retries) when it copied a tag, and none when nothing changed. The body is `{"registry": "<TARGET_REGISTRY>", "images": [{"repository", "tag", "digest"}], "references": "repo:tag\nrepo:tag"}`. The headers are `X-Artifact-Type: container-images-request`, `X-Artifact-Format: json`, `Content-Type: application/json`, `X-Registry` (the body's registry), `X-Image-Count` (the length of `images`), and in CI `X-CI-Project`, `X-CI-Pipeline-Url` and `X-CI-Commit-Sha` (CR and LF stripped). Headers are for routing and display only: the NiFi processor trusts only the body, and the image list never goes in a header (Jetty caps headers near 8 KB). `MIRROR_WEBHOOK_TOKEN` is sent as `Authorization: Bearer`, and `MIRROR_WEBHOOK_CA_BUNDLE` is trusted for it in place of `CA_BUNDLE`. A failed POST prints a warning and the sync still succeeds.

## Preconditions

- Each target registry holds the repositories, or the push account may create them.
- A `--path` destination exists and is mounted; `send` refuses a missing directory.
- In the pipeline: protect the default branch, turn on **Pipelines must succeed**, add a nightly schedule. With `MIRROR_SCAN=true`, the `scan` runner reaches `grype.anchore.io` or `GRYPE_DB_UPDATE_URL`, and a shell runner needs `podman`.

## Ledger

The ledger records what `sync` put in `TARGET_REGISTRY` and what `send` delivered. A missing local ledger starts a new one. In CI it lives in the package registry. GitLab keeps every upload of it: each save deletes uploads older than `MIRROR_LEDGER_KEEP_DAYS` (default `7`) and always keeps the newest two. To roll back, delete the newer uploads under **Deploy > Package registry**.

## Out of scope

- Signing or approving across the link (the `.sha256` catches corruption, not tampering) and removing old tags from either registry.
- Configuring the diode or the bucket. For NiFi: the NiFi processors that export from and push to a registry with no skopeo are in github.com/djlongy/nifi-container-registry-processors, and `nifi/CONTAINER-IMAGES-HIGH-BY-HAND.md` builds a high-side flow around `mirror.py import`.

## Expected result

`sync` ends `synced N image(s) into <host>`, `send` ends `sent: NAME.tar`, and `import` prints `imported: <host>/team/prometheus:v3.13.4@sha256:...`. Check one:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/prometheus:v3.13.4 | sha256sum
```
