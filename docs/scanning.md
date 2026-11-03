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

The source is Grype's own configuration, read from environment variables. Set them as CI/CD variables
(**Settings > CI/CD > Variables**) on the project, its group or the instance (**Admin > CI/CD >
Variables**). No YAML changes. A project variable overrides a group one, which overrides an instance one, so
an airgap group can point at its own copy while the rest keep the default.

| Variable | Default | Set it to |
|---|---|---|
| `GRYPE_DB_UPDATE_URL` | `https://grype.anchore.io/databases` | The listing URL. A URL ending in `.json` is used as is. Any other URL gets `/v6/latest.json` appended |
| `GRYPE_DB_CA_CERT` | system roots | A PEM bundle for a server with a private CA. Use a variable of type **File**, so GitLab writes the PEM to a file and passes its path. A PEM in the repository (`certs/ca-bundle.crt`) also works |
| `GRYPE_DB_MAX_ALLOWED_BUILT_AGE` | `120h` | How old a database may be. Raise it where the copy arrives days late, for example `336h` |
| `GRYPE_DB_VALIDATE_AGE` | `true` | Leave it on. `false` scans with a database of any age and gives no warning |

`scan` runs `grype db update` and stops when it fails, so `GRYPE_DB_REQUIRE_UPDATE_CHECK` changes nothing
here. Each network has one source, so there is no fallback list: set the URL that network can reach.

Choose one source:

1. **Anchore through the proxy (recommended).** Allow `grype.anchore.io` for the runners. The database
   stays current with nothing to maintain.
2. **A GitHub release mirror.** For networks that reach GitHub but not Anchore. A mirror that publishes
   each Anchore build as a GitHub release with a `listing.json` whose `path` names the uploaded archive
   asset, such as [grype-db-mirror](https://github.com/example-org/grype-db-mirror), needs no token when public:

   ```text
   GRYPE_DB_UPDATE_URL=https://github.com/example-org/grype-db-mirror/releases/latest/download/listing.json
   ```

   Grype resolves the archive next to the listing (`releases/latest/download/<archive>`). Use a listing
   whose `path` is the asset name: GitHub replaces `:` in asset names with `.`, so Anchore's own
   `latest.json` names a file the release does not have. A mirror that syncs hourly, as that one does,
   serves a new build at most about 1 hour after Anchore's daily publish (06:53 to 07:47 UTC).

   Behind an allowlist proxy, allow exactly these hosts on port 443 (`CONNECT`):

   | Host | Why |
   |---|---|
   | `github.com` | The listing and the archive URL. Both answer with a redirect |
   | `release-assets.githubusercontent.com` | Where GitHub redirects release downloads |
   | The registry in `SYFT_IMAGE` and `GRYPE_IMAGE` | The scanner images (see [Runner tools](#runner-tools)). Also allow its blob storage host if it redirects layer downloads there |

   `grype.anchore.io` is not needed. Set `GRYPE_CHECK_FOR_APP_UPDATE=false` and
   `SYFT_CHECK_FOR_APP_UPDATE=false`, or each run also tries Anchore's release check (see
   [Closed networks: update checks](#closed-networks-update-checks)). Set `HTTPS_PROXY` on the runner.
   On a shell runner, podman passes the proxy variables into the scanner container by default.
3. **A copy inside a closed network.** Copy two files across, from Anchore or from the GitHub mirror,
   and serve them without authentication.
   1. On a machine that can reach the source, download the listing and the archive it names, and check
      the archive against the listing:

      ```sh
      base=https://github.com/example-org/grype-db-mirror/releases/latest/download
      curl -fsSL -o latest.json "$base/listing.json"
      archive=$(python3 -c 'import json; print(json.load(open("latest.json"))["path"])')
      curl -fsSLO "$base/$archive"
      python3 -c 'import json; print(json.load(open("latest.json"))["checksum"][7:] + "  '"$archive"'")' | sha256sum -c -
      ```

      From Anchore instead: `base=https://grype.anchore.io/databases/v6`, and download `latest.json`
      itself rather than `listing.json`.
   2. Serve both files from one directory named `v6/`. Upload the archive first and `latest.json` last,
      so the listing never names a missing archive:

      ```text
      https://files.example.internal/grype/databases/v6/latest.json
      https://files.example.internal/grype/databases/v6/vulnerability-db_v6.1.10_2026-11-01T00.24.34Z_1793514012.tar.zst
      ```

   3. Set `GRYPE_DB_UPDATE_URL=https://files.example.internal/grype/databases` (without `v6/`), plus
      `GRYPE_DB_CA_CERT` if the server uses a private CA.
   4. Refresh it daily. Grype refuses a database older than `GRYPE_DB_MAX_ALLOWED_BUILT_AGE` (120 hours by
      default). If the copy takes longer to arrive, raise that limit.
4. **A GitLab generic package fed across a one-way link.** A pipeline on the connected side publishes
   the GitHub mirror's release into a GitLab generic package, the bundle crosses, and a pipeline on the
   closed side publishes the same files into a generic package there. Point Grype at the closed side's
   `current` listing:

   ```text
   GRYPE_DB_UPDATE_URL=https://$GRYPE_DB_SCANNER_USERNAME:$GRYPE_DB_SCANNER_TOKEN@gitlab.example.internal/api/v4/projects/<id>/packages/generic/grype-db/current/latest.json
   ```

   A private project answers anonymous reads with 401, so the URL carries a deploy token with only
   `read_package_registry`. Keep both parts in masked CI variables. Grype prints the whole URL in
   `grype db status` (which `scan` runs) and at `-v`, and only GitLab's masking keeps the token out of
   the job log.

On a shell runner, `run_image` passes every `GRYPE_` and `SYFT_` variable into the container.

## Closed networks: update checks

Syft and Grype each ask Anchore for a newer release of themselves on every run. Where Anchore is not
reachable the check fails and logs an error such as
`failed to fetch latest version ... toolbox-data.anchore.io ... no such host`. The scan carries on, but
the line hides real errors. `.gitlab-ci.yml` sets `GRYPE_CHECK_FOR_APP_UPDATE=false`. Set
`SYFT_CHECK_FOR_APP_UPDATE=false` as a CI/CD variable too, on the project or the group.

These checks are separate from the database. `GRYPE_DB_AUTO_UPDATE` and `GRYPE_DB_UPDATE_URL` decide
where the database comes from. `scan` sets `GRYPE_DB_AUTO_UPDATE=false` itself after its one update.

## Runner tools

| Executor | What the runner needs | How `sbom` and `scan` run |
|---|---|---|
| docker | Pull access to `SYFT_IMAGE` and `GRYPE_IMAGE` (mirror them into a registry the runner may pull from) | The job runs inside the image and calls `/syft` or `/grype` directly |
| shell | `podman`, and pull access to the same two images | `run_image` starts the image with `podman run`, mounting the job directory |
| shell, with the binaries | Syft and Grype installed as `/syft` and `/grype` | `run_image` calls them directly and skips podman |

The images are the default because one line in `.gitlab-ci.yml` pins each version for every runner, and
the runner host needs nothing installed. Installed binaries skip the image pull but are kept current per
host, and a Grype too old to read the v6 listing fails `grype db update` with
`no db candidates with correct version available`. Distribution packages often lag that far behind.
`sync` needs neither tool.

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
