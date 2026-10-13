# nifi-container-images

NiFi processors that export container images from a registry into a mirror.py export tar and push such a tar into a registry, digests unchanged, with no skopeo or docker on the NiFi host.

## Requirements

- Apache NiFi 2.x. The NAR is built against NiFi 2.11.0 (`nifi-api` 2.10.0); for another 2.x release set `nifi.version` and `nifi.api.version` in `pom.xml` to what its `lib/` holds.
- Java 21 and Maven 3.9 to build, or Docker to build in a container.
- Any registry that follows the distribution API: `registry:2` (open, htpasswd or token auth), Quay, Artifactory, Harbor, GitLab.

## Build

```sh
docker run --rm -v "$PWD":/src -w /src maven:3.9.11-eclipse-temurin-21 mvn -B package
# nifi-container-images-nar/target/nifi-container-images-nar-1.0.2.nar
```

## Install

Copy the NAR into the directory `nifi.nar.library.autoload.directory` names in `nifi.properties` (`./extensions` when unset) on **every** node. NiFi loads it within a minute, no restart. On Kubernetes that directory is in the container's own filesystem, so a pod restart drops the NAR: bake it into the image (`COPY nifi-container-images-nar-1.0.2.nar /opt/nifi/nifi-current/extensions/`) or mount a volume there. A newer version sits beside the old one; pick it per processor (**Change Version**), and remove the old file once nothing uses it.

```sh
kubectl -n <namespace> cp nifi-container-images-nar-1.0.2.nar <pod>:/opt/nifi/nifi-current/extensions/   # each node
```

## Properties

The full set, with descriptions, is in each processor's **Usage** page in NiFi and in `PushContainerImages.java` and `PullContainerImages.java`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | Registry URL | none | `https://host`, `http://host:5000`, or a path base such as Artifactory's `https://host/artifactory/api/docker/<repo-key>` |
| Optional | Username, Password | none | Robot account, service account or htpasswd user; empty for an open registry |
| Optional | SSL Context Service | Java trust store | Trust store, and keystore for client certificates; Pull also uses it for the Ledger URL |
| Optional | Rewrite Base Path, Strip Base Path | none, `false` | Replace or remove the first path segment of every repository |
| Push | Move Existing Tags | `false` | A tag at another digest is refused, and nothing is pushed, unless `true` |
| Pull | Image Source | `Registry Listing` | `Registry Listing`: every tag of **Repositories** (`team/*`, `*`) whose digest changed since the last export. `Ledger`: what the mirror pipeline's **Ledger URL** says it synced |
| Pull | Tag Filter | `(?!sha256-).+` | Skips signature and attestation tags |
| Pull | Initial Run | `Export All` | With empty state: export everything, or `Record Only` to baseline and export only later changes |
| Pull | Max Export Size | one tar | Split into tars that are each complete; an image over the cap goes alone |

## Usage

Low side, scheduled on the **primary node** (other nodes skip scheduled runs):

```text
PullContainerImages (Registry URL https://quay.low.example.internal, Repositories team/*, Run Schedule 15 min)
  success -> UpdateAttribute (your diode attributes) -> the diode
```

High side, after the diode has reassembled the file:

```text
RouteOnAttribute ${filename:matches('container-images-.*\.tar(\.ffv3)?')}
  -> UnpackContent (flowfile-stream-v3, only for a .ffv3)
  -> PushContainerImages (Registry URL https://registry.high.example.internal, Username, Password)
     success -> done      failure -> a stopped processor, to inspect registry.error
```

A FlowFile whose content lists `team/app:1.0` or `team/app@sha256:...`, one per line, makes PullContainerImages export those on demand, without touching its state.

## Behaviour

- Both read and write the mirror.py export layout: `export.json` plus one `skopeo dir:` copy per image, shared blobs stored once as hard links. `mirror.py import` takes a tar PullContainerImages wrote, and PushContainerImages takes one `mirror.py export` wrote.
- Pull's output carries the attributes `mirror.py send` uses: `filename`, `Filename`, `Content-Type`, `X-Sha256`, `X-Artifact-Type` (`container-images`), `X-Artifact-Format` (`tar`), plus `registry.images`.
- Pull resolves each tag to a digest once and copies that digest, so a tag that moves mid-run is taken next run. A digest the registry no longer holds is skipped with a warning and retried. Any other error emits nothing and records nothing.
- Pull keeps `repository:tag -> digest` in cluster state and records it only once the tars are committed. A rebuilt low registry with the same images exports nothing; clearing the state (**View State > Clear**) exports everything again.
- With `Ledger`, the ledger is read once per run and refused when it names another registry than Registry URL. It is never written.
- Push checks every image against its recorded digest and every target tag before the first upload, skips tags already at their digest and blobs the registry holds, uploads a blob shared by several repositories once and mounts it into the rest, and reads each tag back after the push.

## Preconditions

- The push account may create repositories, or they exist. Quay: a robot with write on the repository or creator on the organization.
- Artifactory: use the per-repository API base `https://<host>/artifactory/api/docker/<repo-key>` as Registry URL. Its repository-path method answers uploads with a Location that drops the repository key.
- The registries, and the Ledger URL, are reachable from every NiFi node.

## Out of scope

- Signing or verifying signatures; scanning.
- Deleting tags or repositories.
- Splitting one image across tars.

## Expected result

Push routes to `success` with `registry.images.pushed` and `registry.images.present`; Pull emits `container-images-<UTC time>.tar`. Check a tag on the far side:

```sh
skopeo inspect --raw docker://registry.high.example.internal/team/app:1.0 | sha256sum
```
