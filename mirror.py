#!/usr/bin/env python3
"""Mirror reviewed container images into a registry, export them as tar files, send the tars across a
one-way link and import them into a registry on the far side."""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
DIGEST = r"sha256:[0-9a-f]{64}"
HOST = r"(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::[0-9]+)?|[a-z0-9-]+:[0-9]+"  # a dot, a port or localhost
SOURCE = re.compile(rf"(?P<host>{HOST})/(?P<repo>[a-z0-9._/-]+):(?P<tag>{TAG})")
TARGET = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+")
REFERENCE = re.compile(rf"(?:(?P<host>{HOST})/)?(?P<repo>[a-z0-9._/-]+?)(?::(?P<tag>{TAG}))?")
IMAGE_CONFIGS = ("application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json")
IMAGE_LAYER = re.compile(r"application/vnd\.oci\.image\.layer\.(?:nondistributable\.)?v1\.tar(?:\+gzip|\+zstd)?"
                         r"|application/vnd\.docker\.image\.rootfs\.(?:foreign\.)?diff\.tar(?:\.gzip)?")
MANIFESTS = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
INDEXES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
CHART_CONFIG = "application/vnd.cncf.helm.config.v1+json"
CHART_LAYERS = ("application/vnd.cncf.helm.chart.content.v1.tar+gzip", "application/vnd.cncf.helm.chart.provenance.v1.prov")
LEDGER_PACKAGE, LEDGER_VERSION, LEDGER_NAME = "registry-mirror-ledger", "v2", "ledger.json"
EXPORT_INDEX = "export.json"


class MirrorError(Exception):
    pass


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise MirrorError(f"{name} is required")
    return value.rstrip("/")


def registry(side):
    value = required_env(side)
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value):
        raise MirrorError(f"{side} must be a registry host[:port], without scheme or path")
    return value


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def parse_image(source, target):
    name, separator, digest = source.partition("@")
    match = SOURCE.fullmatch(name)
    if (not match or not separator or not re.fullmatch(DIGEST, digest)
            or not TARGET.fullmatch(target) or any(p in ("", ".", "..") for p in match['repo'].split('/'))):
        raise MirrorError("expected registry/repo:tag@sha256:<64 hex> org/repo[/path]")
    return {"source": source, "target": target, "tag": match["tag"], "digest": digest}


def catalog(path):
    images, seen = [], set()
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise MirrorError(f"{path}:{number}: expected a pinned source and target")
        image = parse_image(*fields)
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            raise MirrorError(f"{path}:{number}: duplicate target tag {key}")
        seen.add(key)
        images.append(image)
    return images


def say(message):
    if os.environ.get("MIRROR_VERBOSE") == "true":
        # A URL may carry credentials, as --nifi-url can: never print them.
        print(re.sub(r"://[^/@\s]+@", "://", message), file=sys.stderr, flush=True)


def run(*args):
    say("+ " + " ".join(args))
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise MirrorError(f"{args[0]} {args[1]} failed: {result.stderr.decode(errors='replace')[-500:].strip()}")
    return result.stdout


# Credentials come from skopeo's default auth file, written by `skopeo login` or
# `podman login`. REGISTRY_AUTH_FILE moves it for both the login and this script.
def options(side, direction=""):
    return [f"--{direction}tls-verify=false"] if side and os.environ.get(f"{side}_TLS_VERIFY") == "false" else []


def raw_digest(reference, side):
    # A manifest digest is the sha256 of its exact bytes, which --raw returns.
    raw = run("skopeo", "inspect", "--raw", *options(side), reference)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def present_digest(reference, side):
    """The digest a tag points at, or None when the registry has no such tag."""
    try:
        return raw_digest(reference, side)
    except MirrorError as error:
        if re.search(r"manifest unknown|name unknown|not found", str(error), re.I):
            return None
        raise  # a login, TLS or network fault is not proof the tag is absent


def side_of(reference):
    """Which registry's settings read a reference: TARGET_REGISTRY or SOURCE_REGISTRY when its host is
    that registry, else None (defaults: TLS verified, no login)."""
    host = reference.split("://", 1)[-1].split("/", 1)[0]
    for side in ("TARGET_REGISTRY", "SOURCE_REGISTRY"):
        if host and host == os.environ.get(side, "").strip().rstrip("/"):
            return side
    return None


def fetch_manifest(reference, side=None):
    """The manifest bytes at reference@digest, checked against that digest."""
    raw = run("skopeo", "inspect", "--raw", *options(side or side_of(reference)), reference)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != reference.rsplit("@", 1)[1]:
        raise MirrorError(f"manifest digest mismatch: {reference}")
    return raw


def transport_reference(source):
    name, separator, digest = source.partition("@")
    return name.rsplit(":", 1)[0] + "@" + digest if separator else name


def copy(source, destination, source_side=None, destination_side=None):
    args = ["skopeo", "copy", "--all", "--preserve-digests", "--retry-times=3"]
    if source_side:
        args += options(source_side, "src-")
    if destination_side:
        args += options(destination_side, "dest-")
    run(*args, source, destination)


def tls_context(ca=None, verify=True):
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    for bundle in {ca, os.environ.get("CI_SERVER_TLS_CA_FILE")} - {None, ""}:
        context.load_verify_locations(cafile=bundle)
        # Python 3.13+ also demands RFC 5280 extensions an appliance's own CA often lacks (key usage);
        # trust still comes only from this CA.
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


# The ledger: what sync put in TARGET_REGISTRY and what send delivered. In a GitLab CI job it is a file
# in the project's generic package registry, so nothing persists on the runner; elsewhere a local file.

def ledger_location(path):
    """Where the ledger lives: MIRROR_LEDGER (a file, "gitlab", or the https URL of the pipeline's ledger
    file, read only), else the package registry in a CI job, else .mirror-ledger.json beside the catalog."""
    setting = os.environ.get("MIRROR_LEDGER", "").strip()
    if setting in ("gitlab", "true") or (not setting and os.environ.get("CI_JOB_TOKEN")):
        return "gitlab"
    if re.match(r"https?://", setting):
        return setting
    if setting and setting != "false":
        return Path(setting)
    return path.resolve().parent / ".mirror-ledger.json"


def package_request(method, data=None, url=None):
    """The ledger file in the project's generic packages, or at url. PACKAGE_TOKEN (a personal, project
    or group access token) wins; in CI the job token reads and writes the job's own project."""
    api = (os.environ.get("GITLAB_API_URL") or os.environ.get("CI_API_V4_URL") or "").rstrip("/")
    project = os.environ.get("PACKAGE_PROJECT") or os.environ.get("CI_PROJECT_ID") or ""
    token = os.environ.get("PACKAGE_TOKEN")
    key, value = ("PRIVATE-TOKEN", token) if token else ("JOB-TOKEN", os.environ.get("CI_JOB_TOKEN", ""))
    if not value or not (url or (api and project)):
        raise MirrorError("the ledger in the package registry needs PACKAGE_TOKEN, and GITLAB_API_URL and "
                          "PACKAGE_PROJECT or its URL (a CI job supplies all of them)")
    url = url or (f"{api}/projects/{urllib.parse.quote(project, safe='')}/packages/generic/"
                  f"{LEDGER_PACKAGE}/{LEDGER_VERSION}/{LEDGER_NAME}")
    say(f"+ {method} {url}")
    request = urllib.request.Request(url, data=data, method=method, headers={key: value})
    try:
        with urllib.request.urlopen(request, timeout=600, context=tls_context(os.environ.get("CA_BUNDLE"))) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if method == "GET" and error.code == 404:
            return None
        raise MirrorError(f"{method} {url}: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise MirrorError(f"{method} {url}: {error.reason}") from error


def load_ledger(location, low, create=True):
    """The ledger for registry low (any registry when low is empty). None when there is none and create
    is false."""
    if location == "gitlab":
        raw, shown = package_request("GET"), f"package {LEDGER_PACKAGE}/{LEDGER_VERSION}/{LEDGER_NAME}"
    elif isinstance(location, str):  # the pipeline's ledger by URL: read only, so it must exist
        raw, shown = package_request("GET", url=location), location
        if raw is None:
            raise MirrorError(f"no ledger at {location}")
    else:
        raw, shown = (location.read_bytes() if location.exists() else None), str(location)
    if raw is None:
        if not create:
            return None
        print(f"ledger: none at {shown}, starting a new one", file=sys.stderr)
        return {"registry": low, "synced": {}, "sent": {}, "platforms": {}}
    ledger = json.loads(raw)
    for part in ("synced", "sent", "platforms"):
        ledger.setdefault(part, {})
    if low and ledger.get("registry") != low:
        raise MirrorError(f"the ledger at {shown} belongs to {ledger.get('registry')}, not {low}; "
                          "set MIRROR_LEDGER to another file")
    print(f"ledger: {shown}", file=sys.stderr)
    return ledger


def save_ledger(location, ledger):
    if isinstance(location, str) and location != "gitlab":
        # A ledger read by URL belongs to the pipeline: a manual run never changes it.
        say(f"ledger: {location} is read only; not updated")
        return False
    elif location == "gitlab":  # GitLab serves the newest upload of a file name
        package_request("PUT", json.dumps(ledger, indent=2, sort_keys=True).encode())
    else:
        location.parent.mkdir(parents=True, exist_ok=True)
        write_json(location, ledger)
    return True


def normalise(source):
    """Expand a short name the way docker pull does: alpine:3 is docker.io/library/alpine:3.
    Only for references mirror.py reads elsewhere (FROM lines, list files); the catalog holds every
    source in full."""
    first, _, rest = source.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        source = "docker.io/" + source
    host, _, repo = source.partition("/")
    if host == "docker.io" and "/" not in repo:
        source = f"{host}/library/{repo}"
    return source


def runnable(manifest, fetch=None, depth=0):
    """True for an image or an image index; false for signatures and other OCI artifacts.

    A cosign signature uses the OCI image config media type, so an image is also
    recognised by its layers. With fetch, each index child is fetched and checked
    itself; without it, a child is judged by its descriptor.
    """
    if manifest.get("artifactType"):
        return False
    if chart(manifest):
        return True
    if "manifests" in manifest:
        return any(image_child(child, fetch, depth) for child in manifest["manifests"])
    layers = manifest.get("layers")
    return (manifest.get("config", {}).get("mediaType") in IMAGE_CONFIGS and isinstance(layers, list)
            and all(IMAGE_LAYER.fullmatch(str(layer.get("mediaType"))) for layer in layers))


def chart(manifest):
    """A Helm chart pushed to an OCI registry: one chart config, a chart tarball and maybe its provenance."""
    layers = manifest.get("layers")
    kinds = [layer.get("mediaType") for layer in layers] if isinstance(layers, list) else []
    return (manifest.get("config", {}).get("mediaType") == CHART_CONFIG and CHART_LAYERS[0] in kinds
            and all(kind in CHART_LAYERS for kind in kinds))


def referrer(child):
    """A signature or a BuildKit attestation listed beside the images of an index."""
    return bool(child.get("artifactType") or (child.get("platform") or {}).get("os") == "unknown"
                or "vnd.docker.reference.type" in (child.get("annotations") or {}))


def image_child(child, fetch, depth):
    if referrer(child):
        return False
    kind = child.get("mediaType")
    if kind not in (*MANIFESTS, *INDEXES, None):
        return False
    if fetch and depth < 3 and re.fullmatch(DIGEST, str(child.get("digest"))):
        # A cosign signature shares the image manifest media type, so only the child itself can tell.
        return runnable(json.loads(fetch(child["digest"])), fetch, depth + 1)
    return kind in MANIFESTS or (kind is None and bool(child.get("platform")))


def rebase(path, base):
    """path with its first segment replaced by base: team/prom/prometheus, new-org -> new-org/prom/prometheus."""
    if not base:
        return path
    moved = base.strip("/") + "/" + path.split("/", 1)[1]
    if not TARGET.fullmatch(moved):
        raise MirrorError(f"--rewrite-base-path {base}: {path} becomes {moved}, not a valid repository path")
    return moved


def resolve_entry(source, target):
    """Pin source to the digest its tag points at now; returns the catalog image and the tags it publishes."""
    name = source.split("@", 1)[0]
    if not re.match(rf"(?:{HOST})/", name):
        raise MirrorError(f"{source} has no registry host; give the full reference, for example {normalise(source)}")
    if not SOURCE.fullmatch(name) or not TARGET.fullmatch(target):
        raise MirrorError("add needs registry/repo:tag and org/repo[/path]")
    raw = run("skopeo", "inspect", "--raw", *options(side_of(source)), "docker://" + transport_reference(source))
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not runnable(json.loads(raw), lambda child: fetch_manifest(f"docker://{name.rsplit(':', 1)[0]}@{child}")):
        raise MirrorError(f"{name} is a signature, attestation or metadata artifact, not a container image; "
                          "add the tag you run, for example :latest, instead")
    image = parse_image(name + "@" + digest, target)
    if "@" in source and source.split("@", 1)[1] != digest:
        raise MirrorError("source digest does not match the manifest")
    repository = "docker://" + name.rsplit(":", 1)[0]
    tags = publish_tags(image["tag"], platform_digest(f"{repository}@{digest}", digest, side_of(source)),
                        repository, side_of(source))
    return image, tags


def write_catalog(path, lines):
    """Add catalog lines and keep the file sorted a to z by source. Leading comments stay at the
    top; a comment directly above an entry moves with it."""
    header, groups, notes = [], [], []
    for raw in (path.read_text().splitlines() if path.exists() else []):
        text = raw.strip()
        if not groups and (not text or text.startswith("#")):
            header.append(raw)
        elif text.startswith("#"):
            notes.append(raw)
        elif text:
            groups.append((text, notes + [raw]))
            notes = []
    groups += [(line, [line]) for line in lines]
    groups.sort(key=lambda group: group[0])
    groups += [("", notes)] if notes else []  # comments after the last entry stay last
    body = "\n".join(header + [line for _, block in groups for line in block])
    path.write_text(body + "\n" if body else "")


def add_image(path, source, target):
    image, tags = resolve_entry(source, target)
    images = catalog(path) if path.exists() else []
    if any((i['target'], i['tag']) == (target, image['tag']) for i in images):
        raise MirrorError("target tag already exists; edit its reviewed catalog entry")
    write_catalog(path, [f"{image['source']} {target}"])
    print(f"added: {image['source']} -> {target}:{', :'.join(tags)}")


def list_reference(text):
    """An image named in a list file, as docker pull reads it: no registry host means docker.io
    (and library/ for a one-part name), no tag means :latest. The catalog gets the full form."""
    full = normalise(text)
    name, separator, digest = full.partition("@")
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    full = name + separator + digest
    if full != text:
        print(f"expanded: {text} -> {full}")
    return full


def add_list(path, folder, prefix=None):
    """Add every image named in folder/*.txt, one per line. Each target is the source path without
    its registry host, under prefix. What resolves is written even when other lines fail."""
    files = sorted(Path(folder).glob("*.txt"))
    if not files:
        raise MirrorError(f"{folder}: no *.txt files")
    existing = {(i["target"], i["tag"]) for i in (catalog(path) if path.exists() else [])}
    added, failed = [], []
    for file in files:
        for number, raw in enumerate(file.read_text().splitlines(), 1):
            text = raw.split("#", 1)[0].strip()
            if not text:
                continue
            where = f"{file.name}:{number}"
            try:
                source = list_reference(text)
                name = source.split("@", 1)[0]
                repository, tag = name.rsplit(":", 1)
                target = repository.split("/", 1)[1]
                target = f"{prefix.strip('/')}/{target}" if prefix else target
                if (target, tag) in existing:
                    print(f"already in the catalog: {where} {source} -> {target}:{tag}")
                    continue
                image, tags = resolve_entry(source, target)
                existing.add((target, tag))
                added.append(f"{image['source']} {target}")
                print(f"added: {where} {image['source']} -> {target}:{', :'.join(tags)}")
            except MirrorError as error:
                failed.append(f"{where} {text}: {error}")
    if added:
        write_catalog(path, added)
    print(f"{len(added)} added to {path.name}, {len(failed)} failed")
    if failed:
        raise MirrorError(f"{len(failed)} line(s) not added:\n  " + "\n  ".join(failed))


def platform_digest(reference, digest, side):
    """The digest to mirror: the whole image, or one platform's image when MIRROR_PLATFORM is set."""
    wanted = os.environ.get("MIRROR_PLATFORM", "").strip()
    if not wanted:
        return digest
    data = json.loads(run("skopeo", "inspect", "--raw", *options(side), reference))
    keys = ("os", "architecture", "variant")
    want = dict(zip(keys, wanted.split("/")))
    if chart(data):
        return digest  # a chart has no platform
    if "manifests" not in data:
        # A single image goes whole, but only when its config names the wanted platform.
        config = json.loads(run("skopeo", "inspect", "--config", "--raw", *options(side), reference))
        found = {k: config.get(k) for k in want} if isinstance(config, dict) else {}
        if found != want:
            raise MirrorError(f"{reference}: is a {'/'.join(str(v) for v in found.values())} image, not {wanted}")
        return digest
    repository = reference.rsplit("@", 1)[0]

    def platform(child):
        # Checked before the platform field: a referrer or nested index may name a platform too.
        if (child.get("mediaType") not in (*MANIFESTS, None) or referrer(child)
                or not re.fullmatch(DIGEST, str(child.get("digest")))):
            return {}  # a nested index or a referrer: not an image this index can select
        if "platform" in child:
            return child["platform"] or {}
        # The platform is optional in an index; the child image's config names it instead.
        config = json.loads(run("skopeo", "inspect", "--config", "--raw", *options(side),
                                f"{repository}@{child['digest']}"))
        return config if isinstance(config, dict) else {}

    matches = [child["digest"] for child in data["manifests"]
               if {k: platform(child).get(k) for k in want} == want]
    if len(matches) != 1 or not re.fullmatch(DIGEST, matches[0]):
        raise MirrorError(f"{reference}: expected one {wanted} image, found {len(matches)}")
    return matches[0]


def publish_tags(tag, transfer, repository, side):
    """The tags an image gets in the target registry: the source tag, unchanged.

    latest names no release, and once it moves the registry may garbage-collect the untagged
    old image. A latest image is therefore also tagged with the version it reports.
    """
    if tag != "latest":
        return [tag]
    version = app_version(f"{repository}@{transfer}", side)
    return ["latest", *([version] if version not in ("", "latest") else [])]


def app_version(reference, side):
    """The version an image reports about itself, for images published as latest.

    Bitnami's Docker Hub images ship only latest; every other tag is a signature or
    metadata artifact. An image that reports no version gives "". A failed lookup raises,
    so a run never reports success while the version tag is missing.
    """
    wanted = os.environ.get("MIRROR_PLATFORM", "").strip()
    os_name, _, arch = wanted.partition("/")
    override = [f"--override-os={os_name}", f"--override-arch={arch.split('/')[0]}"] if wanted else []
    try:
        if not wanted:
            # Inspect one image of an index, not whichever platform this host happens to run.
            children = json.loads(run("skopeo", "inspect", "--raw", *options(side), reference)).get("manifests")
            images = [child["digest"] for child in children or [] if image_child(child, None, 0)]
            reference = reference.rsplit("@", 1)[0] + "@" + images[0] if images else reference
        data = json.loads(run("skopeo", *override, "inspect", *options(side), reference))
    except MirrorError as error:
        raise MirrorError(f"cannot read the version of {reference}: {error}") from error
    labels = data.get("Labels") or {}
    found = [labels.get("org.opencontainers.image.version"), labels.get("app.kubernetes.io/version")]
    found += [env.split("=", 1)[1] for env in data.get("Env") or [] if env.startswith("APP_VERSION=")]
    return next((v for v in found if v and re.fullmatch(TAG, v)), "")


def pin(image):
    """What the ledger records per catalog tag: the approved digest and the platform choice."""
    return f"{image['digest']} {os.environ.get('MIRROR_PLATFORM', '').strip()}".strip()


def sync(path, ledger_file):
    """Copy every catalog image into TARGET_REGISTRY by digest. Safe to run any number of times: a tag
    already at its digest is left alone. The ledger records what is there, for export."""
    low = registry("TARGET_REGISTRY")
    images = catalog(path)
    if not images:
        raise MirrorError("catalog is empty; add an image before syncing")
    ledger = load_ledger(ledger_file, low)
    loaded = json.dumps(ledger, sort_keys=True)
    known, platforms = ledger.get("platforms", {}), {}
    platform = os.environ.get("MIRROR_PLATFORM", "").strip()
    # A tag a catalog entry pins, or pinned before its line was removed, is never a version tag.
    pinned = ledger.setdefault("pinned", [])
    catalog_tags = {f"{i['target']}:{i['tag']}" for i in images} | set(pinned)
    copied = 0
    for image in images:
        key = f"{image['target']}:{image['tag']}"
        source = transport_reference(image["source"])  # by digest: a moved source tag changes nothing
        # Remember each platform and version-tag choice, so an unchanged image needs no source request.
        lookup = f"{image['digest']} {platform} {image['tag']}"
        cached = known.get(lookup)
        if isinstance(cached, list) and len(cached) == 2 and isinstance(cached[1], list):
            transfer, tags = cached
        else:
            transfer = platform_digest("docker://" + source, image["digest"], side_of(source))
            tags = publish_tags(image["tag"], transfer, "docker://" + source.split("@")[0], side_of(source))
        if tags != ["latest"]:  # a latest image with no version yet is asked again next run
            platforms[lookup] = [transfer, tags]
        for tag in tags[1:]:
            if f"{image['target']}:{tag}" in catalog_tags:
                print(f"warning: {key} reports version {tag}, which images.txt pins or once pinned; not tagging it",
                      file=sys.stderr)
        tags = [tags[0], *[t for t in tags[1:] if f"{image['target']}:{t}" not in catalog_tags]]
        if key not in pinned:
            # Recorded before the first copy, so a run that fails after it still protects the tag.
            pinned.append(key)
            save_ledger(ledger_file, ledger)
        for number, tag in enumerate(tags):
            destination = f"docker://{low}/{image['target']}:{tag}"
            try:
                observed = raw_digest(destination, "TARGET_REGISTRY")
            except MirrorError as error:  # missing, deleted or expired: copy it; a real fault fails the copy
                observed = None
                say(f"not readable in target registry, copying: {error}")
            if observed == transfer:
                say(f"already in target registry: {low}/{image['target']}:{tag}@{transfer}")
                continue
            if number:  # a version tag points at the image already in the target registry
                copy(f"docker://{low}/{image['target']}@{transfer}", destination, "TARGET_REGISTRY", "TARGET_REGISTRY")
            else:
                copy("docker://" + source.split("@")[0] + "@" + transfer, destination, side_of(source), "TARGET_REGISTRY")
            if raw_digest(destination, "TARGET_REGISTRY") != transfer:
                raise MirrorError(f"digest mismatch in target registry: {image['target']}:{tag}")
            print(f"pushed to target registry: {low}/{image['target']}:{tag}@{transfer}")
            copied += 1
        ledger["synced"][key] = {"pin": pin(image), "digest": transfer, "tags": tags}
    ledger["platforms"] = platforms
    if json.dumps(ledger, sort_keys=True) != loaded:
        save_ledger(ledger_file, ledger)
    print(f"synced {len(images)} image(s) into {low}: {copied} tag(s) copied, the rest already there")


def pending(path, ledger_file):
    """Catalog sources not yet in TARGET_REGISTRY at their current digest: what sync copies next."""
    # The verify job runs outside the mirror-low environment, so it may not see TARGET_REGISTRY.
    ledger = load_ledger(ledger_file, os.environ.get("TARGET_REGISTRY", "").strip().rstrip("/"), create=False) or {}
    synced = ledger.get("synced", {})
    for image in catalog(path):
        if (synced.get(f"{image['target']}:{image['tag']}") or {}).get("pin") != pin(image):
            print(image["source"])


def size(text):
    """Bytes from a plain number or a number with KiB, MiB, GiB or TiB."""
    match = re.fullmatch(r"([0-9]+)\s*(KiB|MiB|GiB|TiB)?", text.strip(), re.IGNORECASE)
    if not match or not int(match[1]):
        raise MirrorError(f"--max-size must be bytes or a number with KiB, MiB, GiB or TiB, not {text!r}")
    return int(match[1]) * 1024 ** ["", "kib", "mib", "gib", "tib"].index((match[2] or "").lower())


def image_bytes(reference, side):
    """The bytes skopeo copy --all moves for reference (by digest): each manifest, config and layer once."""
    seen, total = set(), 0
    waiting = [reference]
    while waiting:
        current = waiting.pop()
        raw = run("skopeo", "inspect", "--raw", *options(side), current)
        manifest = json.loads(raw)
        total += len(raw)
        for child in manifest.get("manifests", []):
            if child["digest"] not in seen:
                seen.add(child["digest"])
                waiting.append(current.rsplit("@", 1)[0] + "@" + child["digest"])
        for blob in [manifest.get("config") or {}, *manifest.get("layers", [])]:
            if blob.get("digest") and blob["digest"] not in seen:
                seen.add(blob["digest"])
                total += int(blob.get("size", 0))
    return total


def groups(images, cap):
    """Split images into tars of at most cap bytes, in order. An image larger than cap goes alone."""
    if not cap:
        return [images]
    found, total = [[]], 0
    for image in images:
        need = image_bytes(f"docker://{image['host']}/{image['repo']}@{image['digest']}", side_of(image["host"]))
        if need > cap:
            print(f"{image['host']}/{image['repo']}:{image['tag']} is {need} bytes, over --max-size: it goes in a tar of its own")
        if found[-1] and total + need > cap:
            found.append([])
            total = 0
        found[-1].append(image)
        total += need
    return found


def export_tags(entry):
    """registry/repo:tag, repo:tag or repo (every tag) -> [(host, repo, tag)]. No host: TARGET_REGISTRY."""
    match = REFERENCE.fullmatch(entry.strip())
    if not match or not TARGET.fullmatch(match["repo"]):
        raise MirrorError(f"expected [registry/]repo[/path][:tag], not {entry!r}")
    host = match["host"] or registry("TARGET_REGISTRY")
    if match["tag"]:
        return [(host, match["repo"], match["tag"])]
    listed = json.loads(run("skopeo", "list-tags", *options(side_of(host)), f"docker://{host}/{match['repo']}"))
    return [(host, match["repo"], tag) for tag in sorted(listed.get("Tags") or [])]


def share_blobs(layout, seen):
    """Hardlink a blob already stored for an earlier image, so a shared layer is on disk and in the tar once."""
    for path in sorted(layout.glob("*/*")):
        if not re.fullmatch(r"[0-9a-f]{64}", path.name):
            continue
        first = seen.setdefault(path.name, path)
        if first != path and not path.samefile(first):
            link = path.with_name(path.name + ".link")
            try:
                os.link(first, link)
            except OSError:  # a filesystem without hardlinks (exFAT, some shares) keeps both copies
                continue
            os.replace(link, path)


@contextmanager
def staging_dir(near, override=None):
    """A fresh working directory, removed afterwards whatever happens. It is made in override
    (--staging-dir or MIRROR_STAGING_DIR), else beside near, so it is on the disk that needs the space.
    skopeo's own scratch (TMPDIR) goes in it too, never the system /tmp."""
    parent = Path(override or os.environ.get("MIRROR_STAGING_DIR") or near)
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=".mirror-staging-", dir=parent))
    before = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(directory)
    print(f"staging: {directory} (removed when done)")
    try:
        yield directory
    finally:
        if before is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = before
        shutil.rmtree(directory, ignore_errors=True)


def wanted_images(path, ledger_file, entries, listing, everything):
    """What export copies: the named references, else every catalog image (all), else every catalog
    image sync has put in TARGET_REGISTRY and send has not delivered at that digest."""
    entries = list(entries)
    if listing:
        entries += [line.split("#", 1)[0].strip() for line in Path(listing).read_text().splitlines()]
        entries = [entry for entry in entries if entry]
    if entries:
        return [found for entry in entries for found in export_tags(entry)]
    low = registry("TARGET_REGISTRY")
    ledger = load_ledger(ledger_file, low)
    synced, sent = ledger["synced"], ledger["sent"]
    found = []
    for image in catalog(path):
        record = synced.get(f"{image['target']}:{image['tag']}")
        if not record or record.get("pin") != pin(image):
            how = ("the pipeline has not synced it at this digest; name it to export it anyway"
                   if isinstance(ledger_file, str) else "run sync first")
            print(f"not synced yet, {how}: {image['target']}:{image['tag']}")
            continue
        if everything or sent.get(f"{image['target']}:{image['tag']}") != record["digest"]:
            found += [(low, image["target"], tag) for tag in record["tags"]]
    return found


def export(path, ledger_file, entries=(), listing=None, everything=False, output=None, max_size=None,
           staging=None, base=None):
    """Copy images out of a registry into tar files, each with a .sha256 beside it. Bytes and digests are
    unchanged: every image is a skopeo dir: copy, which keeps a Docker manifest list as it is.

    Reads registries only and changes nothing: send records what was delivered. Returns the tars written.
    """
    wanted = wanted_images(path, ledger_file, entries, listing, everything)
    if not wanted:
        print("nothing new to export")
        return []
    output = Path(output or datetime.now(timezone.utc).strftime("mirror-export-%Y%m%dT%H%M%SZ.tar"))
    if output.suffix != ".tar":
        raise MirrorError(f"{output}: the output must end in .tar")
    images = []
    for host, repository, tag in dict.fromkeys(wanted):
        digest = raw_digest(f"docker://{host}/{repository}:{tag}", side_of(host))
        images.append({"host": host, "repo": repository, "tag": tag, "digest": digest})
    batches = groups(images, size(max_size) if max_size else None)
    names = [output] if len(batches) == 1 else [
        output.with_name(f"{output.stem}-{n:03d}-of-{len(batches):03d}.tar") for n in range(1, len(batches) + 1)]
    for name in names:
        if name.exists() or name.with_name(name.name + ".sha256").exists():
            raise MirrorError(f"{name} or its .sha256 already exists; choose another --output")
    output.parent.mkdir(parents=True, exist_ok=True)
    for name, batch in zip(names, batches):
        write_export(name, batch, staging, base)
    return names


def write_export(tar, images, staging, base):
    """One tar: export.json plus one numbered dir: copy per image. The tar is built in staging and moved
    into place, then its .sha256, so a reader of the output directory never sees half a file."""
    with staging_dir(tar.parent, staging) as work:
        layout = work / "export"
        layout.mkdir()
        entries, seen = [], {}
        for number, image in enumerate(images, 1):
            name = f"{number:06d}"
            copy(f"docker://{image['host']}/{image['repo']}@{image['digest']}", f"dir:{layout / name}",
                 side_of(image["host"]))
            if "sha256:" + sha256(layout / name / "manifest.json") != image["digest"]:
                raise MirrorError(f"digest changed during export: {image['host']}/{image['repo']}:{image['tag']}")
            share_blobs(layout, seen)
            target = rebase(image["repo"], base)
            entries.append({"dir": name, "source": f"{image['host']}/{image['repo']}:{image['tag']}",
                            "target": target, "tag": image["tag"], "digest": image["digest"]})
            print(f"exported: {image['host']}/{image['repo']}:{image['tag']}@{image['digest']}"
                  + (f" as {target}" if target != image["repo"] else ""))
        write_json(layout / EXPORT_INDEX, {"created": datetime.now(timezone.utc).isoformat(), "images": entries})
        built = work / tar.name
        with tarfile.open(built, "w") as stream:  # uncompressed: layers are compressed already
            stream.add(layout, arcname=".")
        checksum = sha256(built)
        sidecar = work / (tar.name + ".sha256")
        sidecar.write_text(f"{checksum}  {tar.name}\n")
        shutil.move(built, tar.with_name(tar.name + ".partial"))
        os.replace(tar.with_name(tar.name + ".partial"), tar)
        shutil.move(sidecar, tar.with_name(sidecar.name + ".partial"))
        os.replace(tar.with_name(sidecar.name + ".partial"), tar.with_name(sidecar.name))
    print(f"wrote {tar} and {tar.name}.sha256 ({len(entries)} image tag(s), {tar.stat().st_size} bytes)")


def checked(tar):
    """tar's sha256, after checking it against the .sha256 beside it."""
    sidecar = tar.with_name(tar.name + ".sha256")
    if not sidecar.exists():
        raise MirrorError(f"{sidecar.name} is missing; it must travel with {tar.name}")
    digest = sha256(tar)
    if (sidecar.read_text().split() or [""])[0] != digest:
        raise MirrorError(f"{tar.name} does not match {sidecar.name}")
    return digest


def read_index(tar):
    """export.json from a tar, without unpacking the images."""
    with tarfile.open(tar, "r:") as stream:
        for member in stream:
            if member.name.lstrip("./") == EXPORT_INDEX and member.isfile():
                index = json.loads(stream.extractfile(member).read())
                if not isinstance(index, dict) or not isinstance(index.get("images"), list):
                    raise MirrorError(f"{tar.name}: invalid {EXPORT_INDEX}")
                return index
    raise MirrorError(f"{tar.name} holds no {EXPORT_INDEX}; is it a mirror.py export?")


def s3_settings(bucket=None):
    """The bucket send and import use: --s3-bucket or S3_BUCKET, at S3_ENDPOINT with the AWS_* keys."""
    settings = {"endpoint": os.environ.get("S3_ENDPOINT", "").rstrip("/"), "bucket": bucket or os.environ.get("S3_BUCKET", ""),
                "access": os.environ.get("AWS_ACCESS_KEY_ID", ""), "secret": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
                "region": os.environ.get("S3_REGION") or "us-east-1", "prefix": os.environ.get("S3_PREFIX", "").strip("/")}
    if not all(settings[k] for k in ("endpoint", "bucket", "access", "secret")):
        raise MirrorError("S3 needs S3_ENDPOINT, a bucket (--s3-bucket or S3_BUCKET), AWS_ACCESS_KEY_ID and "
                          "AWS_SECRET_ACCESS_KEY")
    return settings


def s3_request(s3, method, key="", data=None, length=None, query=None):
    """One path-style request to an S3-compatible store, signed with AWS Signature Version 4. A file-like
    data needs its length, or urllib would send it chunked, which S3 refuses."""
    path = f"/{urllib.parse.quote(s3['bucket'])}" + (f"/{urllib.parse.quote(key)}" if key else "")
    canonical_query = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                               for k, v in sorted((query or {}).items()))
    url = s3["endpoint"] + path + (f"?{canonical_query}" if canonical_query else "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {"host": urllib.parse.urlsplit(url).netloc, "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
               "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, urllib.parse.urlsplit(url).path, canonical_query,
                           *(f"{name}:{headers[name]}" for name in sorted(headers)), "", signed, "UNSIGNED-PAYLOAD"])
    scope = f"{stamp[:8]}/{s3['region']}/s3/aws4_request"
    signing = ("AWS4" + s3["secret"]).encode()
    for part in (stamp[:8], s3["region"], "s3", "aws4_request"):
        signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={s3['access']}/{scope}, SignedHeaders={signed}, "
                                f"Signature={hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()}")
    del headers["host"]  # urllib sends the same value
    if length is not None:
        headers["Content-Length"] = str(length)
    context = tls_context(os.environ.get("S3_CA_BUNDLE"), os.environ.get("S3_TLS_VERIFY") != "false")
    say(f"+ {method} {url}")
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers),
                                      timeout=3600, context=context)
    except urllib.error.HTTPError as error:
        raise MirrorError(f"S3 {method} {s3['bucket']}/{key}: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise MirrorError(f"S3 {method} {s3['bucket']}/{key}: {error.reason}") from error


def s3_key(s3, name):
    return f"{s3['prefix']}/{name}" if s3["prefix"] else name


def s3_list(s3):
    """Every key in the bucket under S3_PREFIX."""
    keys, token = [], None
    while True:
        query = {"list-type": "2", **({"prefix": s3["prefix"] + "/"} if s3["prefix"] else {}),
                 **({"continuation-token": token} if token else {})}
        with s3_request(s3, "GET", query=query) as response:
            root = ElementTree.fromstring(response.read())
        space = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        keys += [item.findtext(f"{space}Key") for item in root.iter(f"{space}Contents")]
        token = root.findtext(f"{space}NextContinuationToken")
        if root.findtext(f"{space}IsTruncated") != "true" or not token:
            return keys


def post_file(path, checksum, url):
    """POST one file to a NiFi ListenHTTP, streamed, with its name and sha256 as headers."""
    parts = urllib.parse.urlsplit(url)
    shown = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()  # no credentials in the log
    file_size = path.stat().st_size
    # HTTP/1.1 keeps the header names as written; HTTP/2 would lowercase them and break a case-sensitive
    # RouteOnAttribute. --upload-file streams the file (--data-binary would read it all into memory),
    # an empty Expect skips the 100-continue round trip, and the time limit allows 1 MB/s. NiFi answers
    # 503 while its queue is full (back pressure); ten retries back off from 1 s to about 8 minutes.
    headers = ["Expect:", f"Filename: {path.name}", "Content-Type: application/octet-stream",
               f"X-Sha256: {checksum}", "X-Artifact-Type: container-images", "X-Artifact-Format: tar"]
    status = run("curl", "--http1.1", "--fail", "--show-error", "--silent", "--connect-timeout", "15",
                 "--max-time", str(max(3600, file_size // 1_000_000)), "--retry", "10", "--request", "POST",
                 "--output", "/dev/null", "--write-out", "%{http_code}",
                 *[item for header in headers for item in ("--header", header)], "--upload-file", str(path), url)
    print(f"posted to NiFi: {path.name} -> {shown} (HTTP {status.decode().strip()}, {file_size} bytes)")


def copy_file(path, directory):
    """Copy under a .partial name, then rename, so a reader of the directory never sees half a file."""
    partial = directory / (path.name + ".partial")
    shutil.copyfile(path, partial)
    os.replace(partial, directory / path.name)


def send(tars, ledger_file, nifi_url=None, s3_bucket=None, directory=None):
    """Deliver each tar, then its .sha256, to one place: a NiFi ListenHTTP, an S3 bucket or a directory
    (an NFS share, a USB stick). Once a tar is delivered its images count as sent in the ledger."""
    if sum(bool(where) for where in (nifi_url, s3_bucket, directory)) != 1:
        raise MirrorError("send needs exactly one of --nifi-url, --s3-bucket or --path")
    s3 = s3_settings(s3_bucket) if s3_bucket else None
    if directory and not Path(directory).is_dir():  # a share that is not mounted must not turn into a local directory
        raise MirrorError(f"{directory} does not exist; mount or create it first")
    digests = {Path(tar): checked(Path(tar)) for tar in tars}  # every tar is checked before the first is sent
    for tar, digest in digests.items():
        sidecar = tar.with_name(tar.name + ".sha256")
        for path in (tar, sidecar):  # the .sha256 last: a tar is complete once its checksum is there
            if nifi_url:
                post_file(path, digest if path is tar else sha256(sidecar), nifi_url)
            elif s3:
                with path.open("rb") as stream:
                    s3_request(s3, "PUT", s3_key(s3, path.name), stream, path.stat().st_size).close()
                print(f"uploaded: {path.name} -> s3 {s3['bucket']}/{s3_key(s3, path.name)}")
            else:
                copy_file(path, Path(directory))
                print(f"copied: {path.name} -> {directory}")
        record_sent(tar, ledger_file)
        print(f"sent: {tar.name}")


def record_sent(tar, ledger_file):
    """Mark the images of a delivered tar as sent, when the ledger belongs to the registry they came from."""
    index = read_index(tar)
    low = os.environ.get("TARGET_REGISTRY", "").strip().rstrip("/")
    ledger = load_ledger(ledger_file, low, create=False) if low else None
    if ledger is None:
        print(f"not recorded: no ledger for {low or 'TARGET_REGISTRY (unset)'}")
        return
    hosts = [image.get("source", "").split("/", 1)[0] for image in index["images"]]
    sent = {image["source"].split("/", 1)[1]: image["digest"] for image, host in zip(index["images"], hosts) if host == low}
    if sent:
        ledger["sent"].update(sent)
        if save_ledger(ledger_file, ledger):
            print(f"ledger: {len(sent)} image tag(s) recorded as sent")
        else:
            print("ledger: read only, the pipeline's ledger is unchanged")


def import_tar(tar, staging=None, force=False, base=None, record=None):
    """Push every image in an export tar into TARGET_REGISTRY, tags and digests unchanged. Every image is
    checked before the first push, so a refusal pushes nothing. A tag already at its digest is skipped."""
    high = registry("TARGET_REGISTRY")
    checked(tar)
    if not hasattr(tarfile, "data_filter"):
        raise MirrorError("import needs Python 3.12, or 3.8.17 to 3.11.4 or later, for safe tar extraction")
    with staging_dir(tar.parent, staging) as layout:
        with tarfile.open(tar, "r:") as stream:  # uncompressed only: the tar's size bounds what unpacks
            stream.extractall(layout, filter="data")
        index = json.loads((layout / EXPORT_INDEX).read_text())
        if not isinstance(index, dict) or not isinstance(index.get("images"), list):
            raise MirrorError(f"invalid {EXPORT_INDEX}")
        plan = []
        for image in index["images"]:
            if (not isinstance(image, dict) or not re.fullmatch(r"[0-9]{6}", str(image.get("dir")))
                    or not TARGET.fullmatch(str(image.get("target"))) or not re.fullmatch(TAG, str(image.get("tag")))
                    or not re.fullmatch(DIGEST, str(image.get("digest")))):
                raise MirrorError(f"invalid entry in {EXPORT_INDEX}: {image!r}")
            directory = layout / image["dir"]
            if "sha256:" + sha256(directory / "manifest.json") != image["digest"]:
                raise MirrorError(f"{image['target']}:{image['tag']} does not match its recorded digest")
            target = rebase(image["target"], base)
            destination = f"docker://{high}/{target}:{image['tag']}"
            present = present_digest(destination, "TARGET_REGISTRY")
            if present and present != image["digest"] and not force:
                raise MirrorError(f"{high}/{target}:{image['tag']} is {present}, the tar has {image['digest']}; "
                                  "a tag does not move without --force")
            plan.append((image, directory, target, destination, present))
        for image, directory, target, destination, present in plan:
            if present == image["digest"]:
                print(f"already there: {high}/{target}:{image['tag']}@{present}")
            else:
                copy(f"dir:{directory}", destination, None, "TARGET_REGISTRY")
                if raw_digest(destination, "TARGET_REGISTRY") != image["digest"]:
                    raise MirrorError(f"digest mismatch in TARGET_REGISTRY after the push: {target}:{image['tag']}")
                print(f"imported: {high}/{target}:{image['tag']}@{image['digest']}")
            if record:
                entries = json.loads(record.read_text()) if record.exists() else []
                record.write_text(json.dumps(entries + [{"target": target, "tag": image["tag"],
                                                         "digest": image["digest"]}], indent=2) + "\n")
    print(f"{tar.name}: {len(plan)} image tag(s) in {high}, digests verified")


def import_all(sources, s3_bucket=None, staging=None, force=False, base=None, record=None, keep=False):
    """Import tar files, every tar in a directory, or every tar in an S3 bucket. A directory or a bucket is
    an inbox: each tar it held is removed once imported, unless keep. A tar named on its own is kept."""
    done = 0
    for source in map(Path, sources):
        if source.is_dir():
            for tar in sorted(source.glob("*.tar")):
                if not tar.with_name(tar.name + ".sha256").exists():
                    print(f"waiting for {tar.name}.sha256; skipped")
                    continue
                import_tar(tar, staging, force, base, record)
                done += 1
                if not keep:
                    tar.unlink()  # the tar first: a .sha256 left alone is never listed
                    tar.with_name(tar.name + ".sha256").unlink()
                    print(f"removed from {source}: {tar.name}")
        else:
            import_tar(source, staging, force, base, record)
            done += 1
    if s3_bucket:
        s3 = s3_settings(s3_bucket)
        keys = set(s3_list(s3))
        for key in sorted(k for k in keys if k.endswith(".tar")):
            if key + ".sha256" not in keys:
                print(f"waiting for {key}.sha256; skipped")
                continue
            with staging_dir(".", staging) as download:
                tar = download / key.rsplit("/", 1)[-1]
                for name, local in ((key, tar), (key + ".sha256", tar.with_name(tar.name + ".sha256"))):
                    with s3_request(s3, "GET", name) as response, local.open("wb") as stream:
                        shutil.copyfileobj(response, stream, 8 * 1024 * 1024)
                import_tar(tar, download, force, base, record)
            done += 1
            if not keep:
                for name in (key, key + ".sha256"):
                    s3_request(s3, "DELETE", name).close()
                print(f"removed from s3 {s3['bucket']}: {key}")
    print(f"imported {done} tar(s)" if done else "nothing to import")


def promote(record, base=None):
    """Copy the images an import recorded from SOURCE_REGISTRY (dev) to TARGET_REGISTRY (prod), by digest.

    The digest is checked in dev before the copy and in prod after it, so prod holds the bytes
    that were imported and approved, whatever the dev tags point at by now.
    """
    dev, prod = registry("SOURCE_REGISTRY"), registry("TARGET_REGISTRY")
    if dev == prod:
        raise MirrorError("SOURCE_REGISTRY and TARGET_REGISTRY are the same registry; nothing to promote")
    images = {}
    for entry in json.loads(record.read_text()):
        if (not isinstance(entry, dict) or not TARGET.fullmatch(str(entry.get("target")))
                or not re.fullmatch(TAG, str(entry.get("tag"))) or not re.fullmatch(DIGEST, str(entry.get("digest")))):
            raise MirrorError(f"{record}: invalid entry {entry!r}")
        images[f"{entry['target']}:{entry['tag']}"] = entry  # a later import's digest for the same tag wins
    if not images:
        print(f"nothing to promote: {record} lists no images")
        return
    for entry in images.values():
        source = f"docker://{dev}/{entry['target']}@{entry['digest']}"
        if raw_digest(source, "SOURCE_REGISTRY") != entry["digest"]:
            raise MirrorError(f"digest mismatch in SOURCE_REGISTRY: {dev}/{entry['target']}@{entry['digest']}")
        key = f"{rebase(entry['target'], base)}:{entry['tag']}"
        destination = f"docker://{prod}/{key}"
        copy(source, destination, "SOURCE_REGISTRY", "TARGET_REGISTRY")
        if raw_digest(destination, "TARGET_REGISTRY") != entry["digest"]:
            raise MirrorError(f"digest mismatch in TARGET_REGISTRY after the copy: {prod}/{key}")
        print(f"promoted: {dev}/{entry['target']}@{entry['digest']} -> {prod}/{key}")
    print(f"promoted {len(images)} image tag(s) from {dev} to {prod}, digests verified")


def references(files):
    """Image references in Containerfiles (FROM) and manifests or rendered charts (image:)."""
    found = []
    for name in files:
        text = sys.stdin.read() if name == "-" else Path(name).read_text()
        stages = set()
        for number, line in enumerate(text.splitlines(), 1):
            match = re.match(r"\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
            if match:
                # An earlier stage, not an image; checked before this line's own alias is known.
                if match[1].lower() not in stages and match[1] != "scratch":
                    found.append((match[1], f"{name}:{number}"))
                if match[2]:
                    stages.add(match[2].lower())
                continue
            match = re.match(r"\s*(?:-\s*)?image:\s*[\"']?([^\"'\s#]+)", line)
            if match:
                found.append((match[1], f"{name}:{number}"))
    return found


def covers(path, files):
    """Fail when a referenced image is not in the catalog, by source or target name."""
    names, targets, digests = set(), set(), set()
    for image in catalog(path):
        names.add(image["source"].split("@")[0])
        targets.add(f"{image['target']}:{image['tag']}")
        digests.add(image["digest"])
    missing = 0
    for reference, where in references(files):
        if "$" in reference:
            print(f"skipped: {reference} ({where}) is built from a variable", file=sys.stderr)
            continue
        name, _, digest = reference.partition("@")
        name = normalise(name)
        if ":" not in name.rsplit("/", 1)[-1]:
            name += ":latest"
        if digest in digests or name in names or name.split("/", 1)[1] in targets:
            continue
        print(f"missing: {reference} ({where})")
        missing += 1
    if missing:
        raise MirrorError(f"{missing} image(s) not in the mirror catalog; add them with mirror.py add")
    print("every image is in the mirror catalog")


# Variables an older release read. Each set one fails with what replaces it, so nothing changes silently.
RENAMED = {
    "LOW_QUAY_HOST": "TARGET_REGISTRY", "HIGH_QUAY_HOST": "TARGET_REGISTRY",
    "LOW_QUAY_USERNAME": "TARGET_REGISTRY_USERNAME", "HIGH_QUAY_USERNAME": "TARGET_REGISTRY_USERNAME",
    "LOW_QUAY_PASSWORD": "TARGET_REGISTRY_PASSWORD", "HIGH_QUAY_PASSWORD": "TARGET_REGISTRY_PASSWORD",
    "LOW_QUAY_TLS_VERIFY": "TARGET_REGISTRY_TLS_VERIFY", "HIGH_QUAY_TLS_VERIFY": "TARGET_REGISTRY_TLS_VERIFY",
    "UPSTREAM_REGISTRY": "SOURCE_REGISTRY", "UPSTREAM_USERNAME": "SOURCE_REGISTRY_USERNAME",
    "UPSTREAM_PASSWORD": "SOURCE_REGISTRY_PASSWORD", "UPSTREAM_TLS_VERIFY": "SOURCE_REGISTRY_TLS_VERIFY",
    "MIRROR_SEND": "NIFI_URL, S3_BUCKET or MIRROR_SEND_PATH (whichever is set says where the pipeline sends)",
    "MIRROR_BUNDLE_MAX_SIZE": "MIRROR_MAX_SIZE (export --max-size)",
    "MIRROR_STAGING_PATH": "MIRROR_STAGING_DIR", "MIRROR_STATE_DIR": "MIRROR_STAGING_DIR",
    "IMPORT_STATE_DIR": "MIRROR_STAGING_DIR", "MIRROR_SCRATCH": "MIRROR_STAGING_DIR",
    "IMPORT_PATH_REWRITE": "MIRROR_REWRITE_BASE_PATH (the new first path segment only)",
    "PROMOTE_PATH_REWRITE": "MIRROR_REWRITE_BASE_PATH (the new first path segment only)",
    "RESEND_ALL": "EXPORT_ALL", "RESEND_SINCE": "EXPORT_IMAGES (export names the images to send again)",
    "RESEND_SEQUENCE": "EXPORT_IMAGES (export names the images to send again)",
    "RESEND_IMAGE": "EXPORT_IMAGES (export names the images to send again)",
    "MIRROR_BUNDLE_DIR": "export --output", "MIRROR_BLOB_DELTA": "nothing: tars carry whole images",
    "IMPORT_STORE": "import DIR or import --s3-bucket", "IMPORT_INBOX_DIR": "import DIR",
    "IMPORT_GAP_GRACE": "nothing: import is idempotent and has no sequence",
    "IMPORT_DELETE_BUNDLES": "nothing: import removes what it imported from a directory or bucket (--keep)",
}


def check_renamed():
    stale = sorted(f"{old} is replaced by {new}" for old, new in RENAMED.items() if os.environ.get(old, "").strip())
    if stale:
        raise MirrorError("unset these variables from an older release: " + "; ".join(stale))


def login():
    """skopeo login to TARGET_REGISTRY, and to SOURCE_REGISTRY when SOURCE_REGISTRY_USERNAME is set.

    A registry without authentication needs no login: leave its USERNAME unset.
    """
    for side in ("TARGET_REGISTRY", "SOURCE_REGISTRY"):
        user = os.environ.get(f"{side}_USERNAME")
        if not user:
            continue
        host = registry(side)
        tls = "false" if os.environ.get(f"{side}_TLS_VERIFY") == "false" else "true"
        result = subprocess.run(["skopeo", "login", f"--tls-verify={tls}", "--username", user, "--password-stdin", host],
                                input=os.environ.get(f"{side}_PASSWORD", "").encode(), capture_output=True, check=False)
        if result.returncode:
            raise MirrorError(f"skopeo login {host} failed: {result.stderr.decode(errors='replace').strip()[-300:]}")
        print(f"logged in to {host} as {user}")


EXAMPLES = """
Low side:
  export TARGET_REGISTRY=registry.low.example.com
  mirror.py sync                                    # copy images.txt into TARGET_REGISTRY
  mirror.py export                                  # what sync added and send has not delivered, as one tar
  mirror.py send mirror-export-*.tar --path /mnt/transfer       # or --nifi-url URL, or --s3-bucket NAME

High side:
  export TARGET_REGISTRY=registry.high.example.com
  mirror.py import /mnt/diode/received              # every tar there, removed once imported
  mirror.py import --s3-bucket received             # every tar in the bucket, removed once imported
  mirror.py import /media/usb/carry.tar             # one tar, kept

Run "mirror.py <command> -h" for each command's options and examples.
"""


def main(argv=None):
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, epilog=EXAMPLES, formatter_class=raw)
    parser.add_argument("--catalog", type=Path, default=ROOT / "images.txt", help="image catalog (default images.txt)")
    parser.add_argument("--ledger", help="ledger file, or the https URL of the pipeline's ledger to read without "
                                         "changing it (or MIRROR_LEDGER; default the package registry in CI, "
                                         "else .mirror-ledger.json beside the catalog)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every skopeo and curl command (or MIRROR_VERBOSE=true)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def command(name, summary, examples):
        return commands.add_parser(name, help=summary, description=summary, epilog="examples:\n" + examples,
                                   formatter_class=raw)

    def staging_flag(sub):
        sub.add_argument("--staging-dir", type=Path,
                         help="where the working copy goes, for a bigger disk (or MIRROR_STAGING_DIR; default beside "
                              "the tar). Always removed afterwards")

    def base_flag(sub):
        sub.add_argument("--rewrite-base-path", metavar="ORG", default=os.environ.get("MIRROR_REWRITE_BASE_PATH"),
                         help="replace the first path segment of every image: team/app -> ORG/app "
                              "(or MIRROR_REWRITE_BASE_PATH)")

    command("sync", "copy every catalog image into TARGET_REGISTRY; safe to run again",
            "  mirror.py sync")
    export_command = command(
        "export", "copy images from a registry into a tar, with a .sha256 beside it",
        "  mirror.py export                                  # what is new since the last send\n"
        "  mirror.py export --all                            # every catalog image\n"
        "  mirror.py export team/prometheus:v3.13.4 team/grafana   # in TARGET_REGISTRY: a tag, every tag\n"
        "  mirror.py export registry.low.example.com/team/app:v1.2.3\n"
        "  mirror.py export --list carry.txt -o /share/carry.tar   # one reference per line\n"
        "  mirror.py export --max-size 4GiB                  # several tars, none over 4 GiB unless one image is\n"
        "  mirror.py export --rewrite-base-path new-org-dev  # team/app lands as new-org-dev/app")
    export_command.add_argument("images", nargs="*", help="registry/repo:tag, or repo:tag in TARGET_REGISTRY; no tag: every tag")
    export_command.add_argument("--list", type=Path, help="a file of image references, one per line, # comments")
    export_command.add_argument("--all", action="store_true", help="every catalog image, not only what is new")
    export_command.add_argument("-o", "--output", type=Path, help="the tar to write (default mirror-export-<UTC time>.tar here)")
    export_command.add_argument("--max-size", default=os.environ.get("MIRROR_MAX_SIZE"),
                                help="split into tars of at most this size, e.g. 4GiB (or MIRROR_MAX_SIZE; default one tar)")
    staging_flag(export_command)
    base_flag(export_command)
    send_command = command(
        "send", "deliver tars and their .sha256 files to NiFi, an S3 bucket or a directory",
        "  mirror.py send mirror-export-*.tar --path /mnt/transfer     # an NFS share or a USB stick\n"
        "  mirror.py send mirror-export-*.tar --nifi-url https://nifi.low:9443/contentListener\n"
        "  mirror.py send mirror-export-*.tar --s3-bucket transfer     # S3_ENDPOINT and AWS_* keys")
    send_command.add_argument("tars", nargs="+", type=Path, help="tar files export wrote")
    where = send_command.add_mutually_exclusive_group(required=True)
    where.add_argument("--nifi-url", help="a NiFi ListenHTTP URL")
    where.add_argument("--s3-bucket", help="a bucket at S3_ENDPOINT (S3_PREFIX optional)")
    where.add_argument("--path", help="a directory; it must exist")
    import_command = command(
        "import", "push the images in export tars into TARGET_REGISTRY, tags and digests unchanged",
        "  mirror.py import /mnt/diode/received          # every tar there; each removed once imported\n"
        "  mirror.py import --s3-bucket received         # every tar in the bucket; each removed once imported\n"
        "  mirror.py import /media/usb/carry.tar         # one tar, checked against carry.tar.sha256, kept\n"
        "  mirror.py import /mnt/diode/received --rewrite-base-path new-org-dev")
    import_command.add_argument("sources", nargs="*", type=Path, help="tar files or directories of them")
    import_command.add_argument("--s3-bucket", help="import every tar in this bucket at S3_ENDPOINT (S3_PREFIX optional)")
    import_command.add_argument("--keep", action="store_true", help="leave imported tars in the directory or bucket")
    import_command.add_argument("--force", action="store_true", help="move a tag that already points at another digest")
    import_command.add_argument("--record", type=Path, help="append the image tags and digests pushed, for promote")
    staging_flag(import_command)
    base_flag(import_command)

    command("login", "log in to TARGET_REGISTRY, and SOURCE_REGISTRY when it has credentials",
            "  TARGET_REGISTRY=registry.example.com TARGET_REGISTRY_USERNAME=robot \\\n"
            "    TARGET_REGISTRY_PASSWORD=... mirror.py login")
    add = command("add", "resolve a tag to its digest and add a catalog entry",
                  "  mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus\n"
                  "  mirror.py add docker.io/bitnamicharts/redis:22.0.7 charts/redis   # an OCI Helm chart")
    add.add_argument("source", help="registry/repository:tag in full, e.g. docker.io/library/alpine:3.20")
    add.add_argument("target", help="repository path in TARGET_REGISTRY, any depth, e.g. team/prometheus")
    add_list_command = command(
        "add-list", "add every image named in a folder of .txt files, keeping images.txt sorted",
        "  mirror.py add-list lists/ --prefix team   # docker.io/prom/prometheus:v3.13.4 -> team/prom/prometheus\n"
        "  A line without a registry host is read as docker pull reads it; without a tag, :latest.")
    add_list_command.add_argument("folder", type=Path, help="a directory of *.txt files")
    add_list_command.add_argument("--prefix", help="repository path the targets go under, e.g. team")
    command("targets", "validate and list the image catalog", "  mirror.py targets")
    command("pending", "list catalog sources sync has not copied yet, one per line", "  mirror.py pending")
    check = command("covers", "fail when a Containerfile or manifest uses an image the catalog lacks",
                    "  mirror.py covers Containerfile\n  helm template chart/ | mirror.py covers -")
    check.add_argument("files", nargs="+", help="Containerfiles, manifests or - for rendered YAML on stdin")
    promote_command = command("promote", "copy recorded images from SOURCE_REGISTRY (dev) to TARGET_REGISTRY (prod) by digest",
                              "  SOURCE_REGISTRY=registry.dev.example.com TARGET_REGISTRY=registry.prod.example.com \\\n"
                              "    mirror.py promote imported.json      # the file import --record wrote")
    promote_command.add_argument("record", type=Path, help="the file import --record wrote")
    base_flag(promote_command)

    args = parser.parse_args(argv)
    if args.verbose:
        os.environ["MIRROR_VERBOSE"] = "true"
    if args.ledger:
        os.environ["MIRROR_LEDGER"] = args.ledger
    try:
        check_renamed()
        ledger_file = ledger_location(args.catalog)
        if args.command == "sync":
            sync(args.catalog, ledger_file)
        elif args.command == "export":
            export(args.catalog, ledger_file, args.images, args.list, args.all, args.output, args.max_size,
                   args.staging_dir, args.rewrite_base_path)
        elif args.command == "send":
            send(args.tars, ledger_file, args.nifi_url, args.s3_bucket, args.path)
        elif args.command == "import":
            if not args.sources and not args.s3_bucket:
                raise MirrorError("import needs a tar, a directory or --s3-bucket")
            import_all(args.sources, args.s3_bucket, args.staging_dir, args.force, args.rewrite_base_path,
                       args.record, args.keep)
        elif args.command == "login":
            login()
        elif args.command == "add":
            add_image(args.catalog, args.source, args.target)
        elif args.command == "add-list":
            add_list(args.catalog, args.folder, args.prefix)
        elif args.command == "targets":
            for image in catalog(args.catalog):
                print(f"{image['source']} -> {image['target']}:{image['tag']}")
        elif args.command == "pending":
            pending(args.catalog, ledger_file)
        elif args.command == "covers":
            covers(args.catalog, args.files)
        else:
            promote(args.record, args.rewrite_base_path)
        return 0
    except (MirrorError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
