# Pin images.txt lines with Renovate

Anyone can add an image to `images.txt` without proxy access: write the line without its digest
and push it.

```text
docker.io/library/alpine:3.20.3 mirror/library/alpine
```

`sync` skips a line with no digest and warns about it. The `pin` job starts a Renovate run on this
project. Renovate looks the digest up through its own proxy and opens one merge request, **Update
images.txt**, with every change to the file: new pins and newer versions, each pinned. Once the
merge request is merged, the next `sync` copies the image. Nothing is copied before a reviewer
approves the digest.

## Setup

Do these once per deployment project. "The runner project" is the project that runs Renovate, for
example `platform/renovate-runner`.

1. **Runner version.** The runner project's `renovate` job must accept a pipeline another project
   starts. Its rule lists `pipeline` among the sources:
   `$CI_PIPELINE_SOURCE =~ /^(schedule|web|api|trigger|pipeline)$/`.
2. **Renovate's token reaches this project.** The runner project's `RENOVATE_TOKEN` is a group
   access token on a group that holds this project. It needs the Developer role and the `api` and
   `write_repository` scopes. A project access token reaches its own project only.
3. **Renovate trusts GitLab's certificate.** If GitLab uses an internal CA, the runner project has
   a `CA_BUNDLE` File variable holding that CA. Without it Renovate stops with `Authentication
   failure`, because it reports a TLS failure as an authentication failure.
4. **Allow this project's job token.** On the runner project, open **Settings > CI/CD > Job token
   permissions** and add this project to the allowlist.
5. **Allow the pipeline variable.** On the runner project, **Settings > CI/CD > Variables >
   Minimum role to use pipeline variables** must be Developer or lower. The `pin` job passes
   `RENOVATE_REPOSITORIES` so that Renovate runs on this project alone.
6. **Give the people who push access to the runner project.** The runner pipeline runs as the
   person whose push started the `pin` job. They need the Developer role on the runner project.
   A group membership that covers both projects is enough.
7. **Point this project at the runner.** In this project, open **Settings > CI/CD > Variables**
   and add `MIRROR_RENOVATE_PROJECT` with the runner project's path, for example
   `platform/renovate-runner`. `MIRROR_RENOVATE_REF` picks the runner branch (default `main`).
8. **Keep `renovate.json`.** The `renovate.json` in this repository makes the digest optional,
   groups every `images.txt` change into one merge request, and rebases it whenever `main` moves.
   With the rebases, `verify` checks the merge request against the current catalog.

## Check it works

1. Push a line without a digest to `main`.
2. In this project's pipeline, the `pin` job log ends with `asked <runner project> to run
   Renovate on <this project>`.
3. The runner project has a new pipeline with source `pipeline`, and its `renovate` job passes.
4. This project has an open merge request **Update images.txt**, whose diff shows the line with
   `@sha256:`. Its `verify` job passes.
5. Merge it. The next `sync` copies the image, and with `MIRROR_REQUEST_URL` set it asks NiFi to
   carry it across.

## Behaviour

- Renovate pins the newest version of each line, not the tag that was written:
  `alpine:3.20.3` can arrive as `alpine:3.24.2@sha256:...`. To mirror exactly the tag you wrote,
  use `mirror.py add`, which needs access to the source registry.
- Two lines for the same image and target can end up on the same version. `verify` then fails
  with `duplicate target tag`. Remove one of the two lines.
- `mirror.py add` replaces a line that has no digest yet with its pinned line.
- `mirror.py list --unpinned` prints the sources that still have no digest.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pin` job: `curl: (60) SSL certificate problem` | The runner does not trust GitLab's CA | Give the runner the CA (`tls-ca-file`, which sets `CI_SERVER_TLS_CA_FILE`) |
| `pin` job: HTTP 403 or 404 | Step 4, 5 or 6 is not done | Add the allowlist entry, the variable role or the membership |
| Runner pipeline starts but has no `renovate` job | Step 1 is not done | Update the runner project |
| `renovate` job: `Authentication failure` | TLS (step 3) or a token that does not reach the project (step 2) | Run the runner pipeline with `LOG_LEVEL=debug` and read the first `GET .../user` |
| No merge request, Dependency Dashboard lists it under Rate-Limited | An hourly limit | `prHourlyLimit: 0`, which this repository's `renovate.json` sets |
| Merged and synced, but the high side never gets it | The low NiFi's Allowed Repositories does not cover the target | Add the target path, or `org/*` for a whole org |
