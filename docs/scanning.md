# SBOM and vulnerability scan

With `MIRROR_SCAN=true`, every image that `sync` is about to copy gets a CycloneDX SBOM (Syft) and a
vulnerability scan (Grype) first. A finding at or above `GRYPE_FAIL_ON` (default `critical`) that has a
fix fails `scan`, and `sync` does not run. A merge request with **Pipelines must succeed** cannot merge.

## What is scanned

- **Which images:** what `mirror.py pending` lists, the `images.txt` lines that `sync` has not copied at
  their current digest. An image already synced is not scanned again by this gate. Two lines with the
  same digest get one SBOM and one scan.
- **From where:** the source registry in `images.txt`, by digest (`registry:docker.io/prom/prometheus@sha256:...`),
  before the copy. The digest is pinned, so these are the bytes that `sync` copies.
- **Charts:** a Helm chart holds no packages. `sbom` logs `chart, no packages to list` and skips it.

## Where the results are kept

| Where | What | How long |
|---|---|---|
| Job artifacts of `sbom` and `scan` | `sbom/sha256-<hex>.cdx.json`, `sbom/sha256-<hex>.grype.json` | 1 week |
| Package registry, package `registry-mirror-sbom`, version `sha256-<hex>` | The same files, uploaded by `sync` after it copies the image | Until deleted |

Find them under **Deploy > Package registry**. Nothing is uploaded when `scan` fails, because `sync` does
not run. `verify` downloads the kept SBOM of an image when there is one, so Syft does not make it again.

## Grype's vulnerability database

Each `scan` job downloads the database once, before it scans. The log shows where it came from and when it
was built (`grype db status`). Without a database the job stops:

```text
no vulnerability database from https://grype.anchore.io/databases: allow it through the proxy or host a copy
```

Choose one:

1. **Through the proxy (recommended).** Allow `grype.anchore.io` for the runners. The database stays
   current with nothing to maintain.
2. **A copy you host.** Use this when the proxy cannot allow it.
   1. On a machine that can reach `grype.anchore.io`, download the listing and the archive it names:

      ```sh
      curl -fsSLO https://grype.anchore.io/databases/v6/latest.json
      curl -fsSLO "https://grype.anchore.io/databases/v6/$(python3 -c 'import json; print(json.load(open("latest.json"))["path"])')"
      ```

   2. Put both files in `v6/` on a web server the runners reach without authentication, for example
      `https://files.example.internal/grype/databases/v6/`.
   3. In this project, set `GRYPE_DB_UPDATE_URL=https://files.example.internal/grype/databases` (without
      `v6/`). If the server uses an internal CA, set `GRYPE_DB_CA_CERT` to a PEM file in the repository,
      such as `certs/ca-bundle.crt`.
   4. Repeat at least every 5 days. Grype refuses a database built more than 120 hours ago
      (`GRYPE_DB_MAX_ALLOWED_BUILT_AGE` changes the limit). A copy refreshed daily keeps the findings current.

On a shell runner, `run_image` passes every `GRYPE_` and `SYFT_` variable into the container.

## Rescan every image

The gate scans each image once, at copy time. A vulnerability published later is found by a rescan:
every `images.txt` image, from its kept SBOM, against the current database. It syncs nothing.

1. **Build > Pipeline schedules > New schedule**, for example nightly at 02:00, on the default branch.
2. Add the variable `MIRROR_RESCAN` = `true`.

A rescan runs `verify`, `sbom` and `scan` only. `sbom` makes an SBOM only for an image that has none kept,
for example one synced before `MIRROR_SCAN` was turned on (it pulls that image from its source). An image whose SBOM cannot be made
(its source unreachable, or the digest gone) does not stop the others: `scan` scans the rest, then fails
and lists it. A finding fails the rescan pipeline and its reports are in the `scan` artifacts. The normal nightly `sync` schedule
is not affected.

## Dependency-Track

Set the project CI variables `DTRACK_URL` (the API base, for example `https://dtrack-api.example.com`) and
`DTRACK_API_KEY` (masked, a key with `BOM_UPLOAD` and `PROJECT_CREATION_UPLOAD`). With `MIRROR_SCAN=true`,
`sync` then also sends each synced image's SBOM to Dependency-Track as project `<repository>` (the
`MIRROR_REGISTRY` path without its host), version `<tag>@<digest>`. Dependency-Track keeps checking them
against new advisories. A failed upload only warns: the SBOM is still in the package registry.
