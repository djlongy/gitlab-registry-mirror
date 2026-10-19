#!/usr/bin/env python3
"""Mirror reviewed container images into a registry, export them as tar files, send the tars across a
one-way link and import them into a registry on the far side."""

import argparse
import atexit
import functools
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
from contextlib import contextmanager, nullcontext
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
VERSION = "0.1.0"


class MirrorError(Exception):
    pass


class UsageError(MirrorError):
    """A setting that is wrong before any work starts: exit 2, like a bad option."""


TRUE_WORDS = ("1", "yes", "y", "true", "t", "on")
FALSE_WORDS = ("0", "no", "n", "false", "f", "off")


def env_flag(name, default=False):
    """A boolean environment variable: 1 yes y true t on, or 0 no n false f off, any case. Unset or empty is
    default; anything else is a usage error."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    raise UsageError(f"{name}={os.environ[name]!r} is not a boolean; use 1, yes, y, true, t, on or 0, no, n, false, f, off")


QUIET = False


def prog():
    """The name this program was started as, so help and messages match how it is run."""
    return Path(sys.argv[0]).name or "registry-mirror"


def progress(message):
    """What the program is doing, on stderr so stdout carries data only. -q silences it."""
    if not QUIET:
        print(message, file=sys.stderr, flush=True)


def warn(message):
    print(f"{prog()}: warning: {message}", file=sys.stderr, flush=True)


def fail(message):
    print(f"{prog()}: {message}", file=sys.stderr, flush=True)


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        flag = {"MIRROR_REGISTRY": "--registry", "MIRROR_SOURCE_REGISTRY": "--source-registry"}.get(name)
        raise MirrorError(f"{flag} or {name} is required" if flag else f"{name} is required")
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


def format_hint():
    return ("each line is: <registry>/<repository>:<tag>@sha256:<64 hex> <org>/<repository>, for example\n"
            "  docker.io/prom/prometheus:v3.5.0@sha256:<digest> team/prom/prometheus\n"
            f"{prog()} add <registry>/<repository>:<tag> <org>/<repository> writes a correct line for you")


def image_problem(source, target, pinned=True):
    """What is wrong with a source and target pair, in words a first-time user can act on, or None."""
    name, separator, digest = source.partition("@")
    match = SOURCE.fullmatch(name)
    if not re.match(rf"(?:{HOST})/", name):
        return f"source {name} has no registry host; give the full reference, for example {normalise(name)}"
    if not match:
        return (f"source {name} is not <registry>/<repository>:<tag>; the repository is lowercase and the tag "
                "comes after a colon")
    if any(part in ("", ".", "..") for part in match["repo"].split("/")):
        return f"source {name} has an empty, . or .. part in its repository path"
    if pinned and not separator:
        return f"source {name} has no @sha256:<digest>; {prog()} add looks the digest up and writes the line"
    if pinned and not re.fullmatch(DIGEST, digest):
        return f"digest {digest} is not sha256: followed by 64 lowercase hex characters"
    if not target:
        return "the target is missing; give the repository path in MIRROR_REGISTRY, for example team/prom/prometheus"
    if not TARGET.fullmatch(target):
        return (f"target {target} is not <org>/<repository>: it needs at least one slash, lowercase letters, "
                "digits and . _ - only, and no tag")
    return None


def parse_image(source, target):
    problem = image_problem(source, target)
    if problem:
        raise MirrorError(problem)
    match = SOURCE.fullmatch(source.partition("@")[0])
    return {"source": source, "target": target, "tag": match["tag"], "digest": source.partition("@")[2]}


def catalog(path):
    """Every image in the catalog. Every bad line is reported at once, by file and line number."""
    images, seen, problems = [], {}, []
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            problems.append(f"{path.name} line {number}: expected 2 fields (source and target), found "
                            f"{len(fields)}: {line}")
            continue
        try:
            image = parse_image(*fields)
        except MirrorError as error:
            problems.append(f"{path.name} line {number}: {error}\n    {line}")
            continue
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            problems.append(f"{path.name} line {number}: duplicate target tag {key}, first on line {seen[key]}")
            continue
        seen[key] = number
        images.append(image)
    if problems:
        raise MirrorError(f"{path} has {len(problems)} invalid line(s); fix or remove them:\n  "
                          + "\n  ".join(problems) + "\n" + format_hint())
    return images


def say(message):
    if env_flag("MIRROR_VERBOSE"):
        # A URL may carry credentials, as --nifi-url can: never print them.
        print(re.sub(r"://[^/@\s]+@", "://", message), file=sys.stderr, flush=True)


def run(*args, live=False):
    """Run a command and return its stdout. With live and -v, its stdout (skopeo copy's blob by blob
    progress) goes straight to stderr instead."""
    say("+ " + " ".join(args))
    shown = live and env_flag("MIRROR_VERBOSE")
    result = subprocess.run(args, stdout=sys.stderr if shown else subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise MirrorError(f"{args[0]} {args[1]} failed: {result.stderr.decode(errors='replace')[-500:].strip()}")
    return result.stdout


# Credentials come from skopeo's default auth file, written by `skopeo login` or
# `podman login`. REGISTRY_AUTH_FILE moves it for both the login and this script.
def options(side, direction="", reference=""):
    if side and not env_flag(f"{side}_TLS_VERIFY", True):
        return [f"--{direction}tls-verify=false"]
    return [f"--{direction}cert-dir={cert_dir(host_of(reference))}"] if ca_files() else []


def host_of(reference):
    return reference.split("://", 1)[-1].split("/", 1)[0]


# Where skopeo looks for a registry's own CA and client certificates, first match wins.
CERTS_D = (Path.home() / ".config/containers/certs.d", Path("/etc/containers/certs.d"), Path("/etc/docker/certs.d"))


@functools.cache
def cert_dir(host):
    """skopeo --cert-dir for host. --cert-dir replaces the host's certs.d directory, so link that
    directory's files in beside MIRROR_CA_BUNDLE: what the system already trusts, and any client certificate,
    keeps working."""
    base = ca_files()[1]
    own = next((d / host for d in CERTS_D if host and (d / host).is_dir()), None)
    if own is None:
        return base
    directory = base / re.sub(r"[^A-Za-z0-9.-]", "_", host)
    directory.mkdir()
    shutil.copyfile(base / "ca.crt", directory / "mirror-ca-bundle.crt")
    for item in own.iterdir():
        (directory / item.name).symlink_to(item.resolve())
    return directory


@functools.cache
def ca_files(variable="MIRROR_CA_BUNDLE"):
    """MIRROR_CA_BUNDLE, a PEM file of one or more CA (or self-signed server) certificates. Every connection
    trusts it in addition to what the system already trusts, never instead: (the file, a skopeo cert dir,
    curl's trust arguments). None when unset."""
    setting = os.environ.get(variable, "").strip()
    if not setting:
        return None
    bundle = Path(setting).resolve()
    if not bundle.is_file():
        raise MirrorError(f"{variable} {setting} is not a file")
    directory = Path(tempfile.mkdtemp(prefix="mirror-ca-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    shutil.copyfile(bundle, directory / "ca.crt")  # skopeo adds every *.crt in --cert-dir to the system roots
    # curl --cacert replaces the system roots, so carry them: their file, or their directory with --capath.
    system = ssl.get_default_verify_paths()
    roots = Path(system.cafile).read_bytes() if system.cafile and Path(system.cafile).is_file() else b""
    (directory / "curl.pem").write_bytes(roots + b"\n" + bundle.read_bytes())
    capath = [] if roots or not system.capath or not Path(system.capath).is_dir() else ["--capath", system.capath]
    return bundle, directory, ["--cacert", str(directory / "curl.pem"), *capath]


def raw_digest(reference, side):
    # A manifest digest is the sha256 of its exact bytes, which --raw returns.
    raw = run("skopeo", "inspect", "--raw", *options(side, reference=reference), reference)
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
    """Which registry's settings read a reference: MIRROR_REGISTRY or MIRROR_SOURCE_REGISTRY when its host is
    that registry, else None (defaults: TLS verified, no login)."""
    host = reference.split("://", 1)[-1].split("/", 1)[0]
    for side in ("MIRROR_REGISTRY", "MIRROR_SOURCE_REGISTRY"):
        if host and host == os.environ.get(side, "").strip().rstrip("/"):
            return side
    return None


def fetch_manifest(reference, side=None):
    """The manifest bytes at reference@digest, checked against that digest."""
    raw = run("skopeo", "inspect", "--raw", *options(side or side_of(reference), reference=reference), reference)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != reference.rsplit("@", 1)[1]:
        raise MirrorError(f"manifest digest mismatch: {reference}")
    return raw


def transport_reference(source):
    name, separator, digest = source.partition("@")
    return name.rsplit(":", 1)[0] + "@" + digest if separator else name


def copy(source, destination, source_side=None, destination_side=None):
    args = ["skopeo", "copy", "--all", "--preserve-digests", "--retry-times=3"]
    if source.startswith("docker://"):
        args += options(source_side, "src-", source)
    if destination.startswith("docker://"):
        args += options(destination_side, "dest-", destination)
    run(*args, source, destination, live=True)


def tls_context(verify=True):
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    for bundle in {ca_files() and str(ca_files()[0]), os.environ.get("CI_SERVER_TLS_CA_FILE")} - {None, ""}:
        context.load_verify_locations(cafile=bundle)
        # Python 3.13+ also demands RFC 5280 extensions an appliance's own CA often lacks (key usage);
        # trust still comes only from this CA.
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context


# The ledger: what sync put in MIRROR_REGISTRY and what send delivered. In a GitLab CI job it is a file
# in the project's generic package registry, so nothing persists on the runner; elsewhere a local file.

def ledger_location(path):
    """Where the ledger lives: MIRROR_LEDGER (a file, "gitlab", or the https URL of the pipeline's ledger
    file, read only), else the package registry in a CI job, else .mirror-ledger.json beside the catalog."""
    setting = os.environ.get("MIRROR_LEDGER", "").strip()
    word = setting.lower()
    if re.match(r"https?://", setting):
        return setting
    if word == "gitlab" or word in TRUE_WORDS or (not setting and os.environ.get("CI_JOB_TOKEN")):
        return "gitlab"
    if setting and word not in FALSE_WORDS:
        return Path(setting)
    return path.resolve().parent / ".mirror-ledger.json"


def package_request(method, data=None, url=None):
    """The ledger file in the project's generic packages, or url (a full URL, or a path under the
    project's packages API). MIRROR_LEDGER_TOKEN (a personal, project
    or group access token) wins; in CI the job token reads and writes the job's own project."""
    api = (os.environ.get("MIRROR_GITLAB_API_URL") or os.environ.get("CI_API_V4_URL") or "").rstrip("/")
    project = os.environ.get("MIRROR_LEDGER_PROJECT") or os.environ.get("CI_PROJECT_ID") or ""
    token = os.environ.get("MIRROR_LEDGER_TOKEN")
    key, value = ("PRIVATE-TOKEN", token) if token else ("JOB-TOKEN", os.environ.get("CI_JOB_TOKEN", ""))
    absolute = bool(url) and url.startswith("http")
    if not value or not (absolute or (api and project)):
        raise MirrorError("the ledger in the package registry needs MIRROR_LEDGER_TOKEN, and MIRROR_GITLAB_API_URL and "
                          "MIRROR_LEDGER_PROJECT or its URL (a CI job supplies all of them)")
    if not absolute:  # a path under the project's packages API; none means the ledger file
        url = (f"{api}/projects/{urllib.parse.quote(project, safe='')}/packages"
               + (url or f"/generic/{LEDGER_PACKAGE}/{LEDGER_VERSION}/{LEDGER_NAME}"))
    say(f"+ {method} {url}")
    request = urllib.request.Request(url, data=data, method=method, headers={key: value})
    try:
        with urllib.request.urlopen(request, timeout=600, context=tls_context()) as response:
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
        progress(f"ledger: none at {shown}, starting a new one")
        return {"registry": low, "synced": {}, "sent": {}, "requested": {}, "platforms": {}}
    ledger = json.loads(raw)
    for part in ("synced", "sent", "requested", "platforms"):
        ledger.setdefault(part, {})
    if low and ledger.get("registry") != low:
        raise MirrorError(f"the ledger at {shown} belongs to {ledger.get('registry')}, not {low}; "
                          "set MIRROR_LEDGER to another file")
    progress(f"ledger: {shown}")
    return ledger


def save_ledger(location, ledger):
    if isinstance(location, str) and location != "gitlab":
        # A ledger read by URL belongs to the pipeline: a manual run never changes it.
        say(f"ledger: {location} is read only; not updated")
        return False
    elif location == "gitlab":  # GitLab serves the newest upload of a file name
        package_request("PUT", json.dumps(ledger, indent=2, sort_keys=True).encode())
        prune_ledger()
    else:
        location.parent.mkdir(parents=True, exist_ok=True)
        write_json(location, ledger)
    return True


def prune_ledger(now=None):
    """Delete uploads of the CI ledger older than MIRROR_LEDGER_KEEP_DAYS (default 7), always keeping the
    newest two. GitLab keeps every upload; an older one is a rollback point. A token that may not delete
    package files leaves them with a warning."""
    days = float(os.environ.get("MIRROR_LEDGER_KEEP_DAYS") or 7)
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - days * 86400
    try:
        found = json.loads(package_request("GET", url=f"?package_type=generic&package_name={LEDGER_PACKAGE}") or b"[]")
        uploads = []
        for package in found:
            if package["name"] != LEDGER_PACKAGE or package["version"] != LEDGER_VERSION:
                continue
            page = 1
            while True:
                batch = json.loads(package_request(
                    "GET", url=f"/{package['id']}/package_files?per_page=100&page={page}") or b"[]")
                uploads += [(f["created_at"], f["id"], package["id"]) for f in batch if f["file_name"] == LEDGER_NAME]
                if len(batch) < 100:
                    break
                page += 1
        uploads.sort(reverse=True)
        old = [u for u in uploads[2:]
               if datetime.fromisoformat(u[0].replace("Z", "+00:00")).timestamp() < cutoff]
        for _, file, package in old:
            package_request("DELETE", url=f"/{package}/package_files/{file}")
        if old:
            progress(f"ledger: pruned {len(old)} upload(s) older than {days:g} day(s)")
    except MirrorError as error:
        warn(f"old ledger uploads not pruned: {error}")


def normalise(source):
    """Expand a short name the way docker pull does: alpine:3 is docker.io/library/alpine:3.
    Only for references this program reads elsewhere (FROM lines, list files); the catalog holds every
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


REPOSITORY = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*")


def rebase(path, base):
    """path with its first segment replaced by base (team/prom/prometheus, new-org -> new-org/prom/prometheus),
    or with its first base leading segments removed when base is a number (2 -> prometheus)."""
    if not base:
        return path
    if isinstance(base, int):
        parts = path.split("/")
        if len(parts) <= base:
            raise MirrorError(f"--strip-components {base}: {path} has no path left after {base} component(s)")
        moved = "/".join(parts[base:])
        if not REPOSITORY.fullmatch(moved):
            raise MirrorError(f"--strip-components {base}: {path} becomes {moved}, not a valid repository path")
        return moved
    moved = base.strip("/") + "/" + path.split("/", 1)[-1]
    if not REPOSITORY.fullmatch(moved):
        raise MirrorError(f"--rewrite-base-path {base}: {path} becomes {moved}, not a valid repository path")
    return moved


def list_lines(file):
    """The lines of a list file ("-" is stdin), without # comments and blanks: [(name:line, text)]."""
    name = "stdin" if str(file) == "-" else Path(file).name
    text = sys.stdin.read() if str(file) == "-" else Path(file).read_text(encoding="utf-8-sig")
    lines = ((number, raw.split("#", 1)[0].strip()) for number, raw in enumerate(text.splitlines(), 1))
    return [(f"{name}:{number}", line) for number, line in lines if line]


def strip_count(text):
    """An argparse type for --strip-components: a whole number of at least 1."""
    if not re.fullmatch(r"[0-9]+", text.strip()) or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of at least 1")
    return int(text)


def resolve_entry(source, target):
    """Pin source to the digest its tag points at now; returns the catalog image and the tags it publishes."""
    name = source.split("@", 1)[0]
    problem = image_problem(source, target, pinned=False)
    if problem:
        raise MirrorError(problem)
    raw = run("skopeo", "inspect", "--raw", *options(side_of(source), reference=source), "docker://" + transport_reference(source))
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
    for raw in (path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []):
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
    progress(f"checking {path}")
    images = catalog(path) if path.exists() else []
    progress(f"resolving {source} to the digest its tag points at now")
    image, tags = resolve_entry(source, target)
    progress(f"digest: {image['digest']}")
    same = [i for i in images if i["target"] == target and i["tag"] == image["tag"]]
    if same:
        raise MirrorError(f"{target}:{image['tag']} is already in {path.name} as {same[0]['source']}; "
                          "edit that line to move it to another digest")
    shared = [f"{i['target']}:{i['tag']}" for i in images if i["digest"] == image["digest"]]
    if shared:
        progress(f"same digest as {', '.join(shared)}: the registry stores the image once and adds this tag")
    write_catalog(path, [f"{image['source']} {target}"])
    progress(f"added to {path.name}: {image['source']} {target}")
    host = os.environ.get("MIRROR_REGISTRY", "").strip().rstrip("/")
    progress(f"sync pushes it as {host + '/' if host else ''}{target}:{', :'.join(tags)}")


def list_reference(text):
    """An image named in a list file, as docker pull reads it: no registry host means docker.io
    (and library/ for a one-part name), no tag means :latest. The catalog gets the full form."""
    full = normalise(text)
    name, separator, digest = full.partition("@")
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    full = name + separator + digest
    if full != text:
        progress(f"expanded: {text} -> {full}")
    return full


def add_files(path, files, prefix=None):
    """Add every image named in the list files ("-" is stdin), one per line. Each target is the source path
    without its registry host, under prefix. What resolves is written even when other lines fail."""
    existing = {(i["target"], i["tag"]) for i in (catalog(path) if path.exists() else [])}
    added, failed = [], []
    for where, text in (line for file in files for line in list_lines(file)):
        try:
            source = list_reference(text)
            name = source.split("@", 1)[0]
            repository, tag = name.rsplit(":", 1)
            target = repository.split("/", 1)[1]
            target = f"{prefix.strip('/')}/{target}" if prefix else target
            if (target, tag) in existing:
                progress(f"already in the catalog: {where} {source} -> {target}:{tag}")
                continue
            image, tags = resolve_entry(source, target)
            existing.add((target, tag))
            added.append(f"{image['source']} {target}")
            progress(f"added: {where} {image['source']} -> {target}:{', :'.join(tags)}")
        except MirrorError as error:
            failed.append(f"{where} {text}: {error}")
    if added:
        write_catalog(path, added)
    progress(f"{len(added)} added to {path.name}, {len(failed)} failed")
    if failed:
        raise MirrorError(f"{len(failed)} line(s) not added:\n  " + "\n  ".join(failed))


def platform_digest(reference, digest, side):
    """The digest to mirror: the whole image, or one platform's image when MIRROR_PLATFORM is set."""
    wanted = os.environ.get("MIRROR_PLATFORM", "").strip()
    if not wanted:
        return digest
    data = json.loads(run("skopeo", "inspect", "--raw", *options(side, reference=reference), reference))
    keys = ("os", "architecture", "variant")
    want = dict(zip(keys, wanted.split("/")))
    if chart(data):
        return digest  # a chart has no platform
    if "manifests" not in data:
        # A single image goes whole, but only when its config names the wanted platform.
        config = json.loads(run("skopeo", "inspect", "--config", "--raw", *options(side, reference=reference), reference))
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
        config = json.loads(run("skopeo", "inspect", "--config", "--raw", *options(side, reference=repository),
                                f"{repository}@{child['digest']}"))
        return config if isinstance(config, dict) else {}

    matches = [child["digest"] for child in data["manifests"]
               if {k: platform(child).get(k) for k in want} == want]
    if len(matches) != 1 or not re.fullmatch(DIGEST, matches[0]):
        raise MirrorError(f"{reference}: expected one {wanted} image, found {len(matches)}")
    return matches[0]


def publish_tags(tag, transfer, repository, side):
    """The tags an image gets in MIRROR_REGISTRY: the source tag, unchanged.

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
            children = json.loads(run("skopeo", "inspect", "--raw", *options(side, reference=reference), reference)).get("manifests")
            images = [child["digest"] for child in children or [] if image_child(child, None, 0)]
            reference = reference.rsplit("@", 1)[0] + "@" + images[0] if images else reference
        data = json.loads(run("skopeo", *override, "inspect", *options(side, reference=reference), reference))
    except MirrorError as error:
        raise MirrorError(f"cannot read the version of {reference}: {error}") from error
    labels = data.get("Labels") or {}
    found = [labels.get("org.opencontainers.image.version"), labels.get("app.kubernetes.io/version")]
    found += [env.split("=", 1)[1] for env in data.get("Env") or [] if env.startswith("APP_VERSION=")]
    return next((v for v in found if v and re.fullmatch(TAG, v)), "")


def pin(image):
    """What the ledger records per catalog tag: the approved digest and the platform choice."""
    return f"{image['digest']} {os.environ.get('MIRROR_PLATFORM', '').strip()}".strip()


def sync(path, ledger_file, dry_run=False):
    """Copy every catalog image into MIRROR_REGISTRY by digest. Safe to run any number of times: a tag
    already at its digest is left alone. The ledger records what is there, for export. With dry_run it reads
    both registries, prints what it would copy and changes nothing: no copy, no ledger save."""
    low = registry("MIRROR_REGISTRY")
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
    copied, changed = 0, []
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
                warn(f"{key} reports version {tag}, which images.txt pins or once pinned; not tagging it")
        tags = [tags[0], *[t for t in tags[1:] if f"{image['target']}:{t}" not in catalog_tags]]
        if key not in pinned:
            # Recorded before the first copy, so a run that fails after it still protects the tag.
            pinned.append(key)
            if not dry_run:
                save_ledger(ledger_file, ledger)
        for number, tag in enumerate(tags):
            destination = f"docker://{low}/{image['target']}:{tag}"
            try:
                observed = raw_digest(destination, "MIRROR_REGISTRY")
            except MirrorError as error:  # missing, deleted or expired: copy it; a real fault fails the copy
                observed = None
                say(f"not readable in MIRROR_REGISTRY, copying: {error}")
            if observed == transfer:
                progress(f"already in MIRROR_REGISTRY: {low}/{image['target']}:{tag}@{transfer}")
                continue
            if dry_run:
                print(f"would copy: {source.split('@')[0]}@{transfer} -> {low}/{image['target']}:{tag}")
                copied += 1
                continue
            progress(f"copying: {source.split('@')[0]}@{transfer} -> {low}/{image['target']}:{tag}"
                     + (f" (target has {observed})" if observed else ""))
            if number:  # a version tag points at the image already in MIRROR_REGISTRY
                copy(f"docker://{low}/{image['target']}@{transfer}", destination, "MIRROR_REGISTRY", "MIRROR_REGISTRY")
            else:
                copy("docker://" + source.split("@")[0] + "@" + transfer, destination, side_of(source), "MIRROR_REGISTRY")
            if raw_digest(destination, "MIRROR_REGISTRY") != transfer:
                raise MirrorError(f"digest mismatch in MIRROR_REGISTRY: {image['target']}:{tag}")
            progress(f"pushed to MIRROR_REGISTRY: {low}/{image['target']}:{tag}@{transfer}")
            copied += 1
            changed.append({"repository": image["target"], "tag": tag, "digest": transfer})
        ledger["synced"][key] = {"pin": pin(image), "digest": transfer, "tags": tags}
    ledger["platforms"] = platforms
    if json.dumps(ledger, sort_keys=True) != loaded and not dry_run:
        save_ledger(ledger_file, ledger)
    if dry_run:
        progress(f"dry run: {copied} tag(s) of {len(images)} image(s) would be copied into {low}, nothing changed")
        return []
    progress(f"synced {len(images)} image(s) into {low}: {copied} tag(s) copied, the rest already there")
    return changed


def pending(path, ledger_file):
    """Catalog sources not yet in MIRROR_REGISTRY at their current digest: what sync copies next."""
    # The verify job runs outside the mirror-low environment, so it may not see MIRROR_REGISTRY.
    ledger = load_ledger(ledger_file, os.environ.get("MIRROR_REGISTRY", "").strip().rstrip("/"), create=False) or {}
    synced = ledger.get("synced", {})
    for image in catalog(path):
        if (synced.get(f"{image['target']}:{image['tag']}") or {}).get("pin") != pin(image):
            print(image["source"])


SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4,
              "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
              "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def size(text):
    """Bytes from a plain number, or a number with a coreutils suffix: K M G T and KiB MiB GiB TiB are
    powers of 1024, KB MB GB TB powers of 1000. An argparse type, so a bad size is a usage error."""
    match = re.fullmatch(r"([0-9]+)\s*([KMGT]|[KMGT]i?B)?", text.strip(), re.IGNORECASE)
    if not match or not int(match[1]):
        raise argparse.ArgumentTypeError(f"{text!r} is not a size: use bytes, or a number with K, M, G, T, "
                                         "KiB, MiB, GiB, TiB (powers of 1024) or KB, MB, GB, TB (powers of 1000)")
    return int(match[1]) * SIZE_UNITS[(match[2] or "").lower()]


def image_bytes(reference, side):
    """The bytes skopeo copy --all moves for reference (by digest): each manifest, config and layer once."""
    seen, total = set(), 0
    waiting = [reference]
    while waiting:
        current = waiting.pop()
        raw = run("skopeo", "inspect", "--raw", *options(side, reference=current), current)
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
            progress(f"{image['host']}/{image['repo']}:{image['tag']} is {need} bytes, over --max-size: it goes in a tar of its own")
        if found[-1] and total + need > cap:
            found.append([])
            total = 0
        found[-1].append(image)
        total += need
    return found


def export_tags(entry):
    """registry/repo:tag, repo:tag or repo (every tag) -> [(host, repo, tag)]. No host: MIRROR_REGISTRY."""
    match = REFERENCE.fullmatch(entry.strip())
    if not match or not TARGET.fullmatch(match["repo"]):
        raise MirrorError(f"expected [registry/]repo[/path][:tag], not {entry!r}")
    host = match["host"] or registry("MIRROR_REGISTRY")
    if match["tag"]:
        return [(host, match["repo"], match["tag"])]
    listed = json.loads(run("skopeo", "list-tags", *options(side_of(host), reference=host), f"docker://{host}/{match['repo']}"))
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
    progress(f"staging: {directory} (removed when done)")
    try:
        yield directory
    finally:
        if before is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = before
        shutil.rmtree(directory, ignore_errors=True)


def named(text):
    """The references a line names: an images.txt line (source and target) names its target:tag in
    MIRROR_REGISTRY, anything else is references separated by spaces."""
    fields = text.split()
    if len(fields) == 2 and not image_problem(*fields, pinned=False):
        return [f"{fields[1]}:{SOURCE.fullmatch(fields[0].partition('@')[0])['tag']}"]
    return fields


def wanted_images(path, ledger_file, entries, files_from, everything, done="sent"):
    """What export (or request) takes: the named references, else every catalog image (all), else every
    catalog image sync has put in MIRROR_REGISTRY that the ledger's done part (sent, or requested) does
    not hold at that digest."""
    entries = [name for text in entries for name in named(text)]
    entries += [name for file in files_from or () for _, text in list_lines(file) for name in named(text)]
    if entries:
        return [found for entry in entries for found in export_tags(entry)]
    low = registry("MIRROR_REGISTRY")
    ledger = load_ledger(ledger_file, low)
    synced, sent = ledger["synced"], ledger[done]
    found = []
    for image in catalog(path):
        record = synced.get(f"{image['target']}:{image['tag']}")
        if not record or record.get("pin") != pin(image):
            how = ("the pipeline has not synced it at this digest; name it to export it anyway"
                   if isinstance(ledger_file, str) else "run sync first")
            progress(f"not synced yet, {how}: {image['target']}:{image['tag']}")
            continue
        if everything or sent.get(f"{image['target']}:{image['tag']}") != record["digest"]:
            found += [(low, image["target"], tag) for tag in record["tags"]]
        else:
            # The ledger keys on target path and digest, not source: a new source at a sent digest is sent.
            progress(f"already {done} at {record['digest']}, skipped (send again: --all or its name, in a pipeline MIRROR_RESEND_ALL or MIRROR_RESEND_IMAGES): "
                     f"{image['target']}:{image['tag']}")
    return found


def export(path, ledger_file, entries=(), files_from=None, everything=False, output=None, max_size=None,
           staging=None, base=None, dry_run=False):
    """Copy images out of a registry into tar files, each with a .sha256 beside it. Bytes and digests are
    unchanged: every image is a skopeo dir: copy, which keeps a Docker manifest list as it is.

    Reads registries only and changes nothing: send records what was delivered. Returns the tars written.
    With output "-" the one finished tar is written to stdout instead, with no .sha256 (the reader checksums
    the stream) and every message on stderr.
    """
    to_stdout = str(output) == "-"
    wanted = wanted_images(path, ledger_file, entries, files_from, everything)
    if not wanted:
        progress("nothing new to export")
        return []
    output = Path(datetime.now(timezone.utc).strftime("container-images-%Y%m%dT%H%M%SZ.tar") if to_stdout or not output
                  else output)
    if output.suffix != ".tar":
        raise MirrorError(f"{output}: the output must end in .tar")
    images = []
    for host, repository, tag in dict.fromkeys(wanted):
        digest = raw_digest(f"docker://{host}/{repository}:{tag}", side_of(host))
        images.append({"host": host, "repo": repository, "tag": tag, "digest": digest})
    batches = groups(images, max_size)
    names = [output] if len(batches) == 1 else [
        output.with_name(f"{output.stem}-{n:03d}-of-{len(batches):03d}.tar") for n in range(1, len(batches) + 1)]
    for name in () if to_stdout else names:
        if name.exists() or name.with_name(name.name + ".sha256").exists():
            raise MirrorError(f"{name} or its .sha256 already exists; choose another --output")
    if dry_run:
        for name, batch in zip(names, batches):
            print(f"would write: {name} and {name.name}.sha256 ({len(batch)} image tag(s))")
            for image in batch:
                print(f"  {image['host']}/{image['repo']}:{image['tag']}@{image['digest']}")
        return []
    if not to_stdout:
        output.parent.mkdir(parents=True, exist_ok=True)
    for name, batch in zip(names, batches):
        write_export(name, batch, staging, base, to_stdout)
    return names


def write_export(tar, images, staging, base, to_stdout=False):
    """One tar: export.json plus one numbered dir: copy per image. The tar is built in staging and moved
    into place, then its .sha256, so a reader of the output directory never sees half a file. With
    to_stdout the finished tar is copied to stdout instead and no .sha256 is written."""
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
            progress(f"exported: {image['host']}/{image['repo']}:{image['tag']}@{image['digest']}"
                     + (f" as {target}" if target != image["repo"] else ""))
        write_json(layout / EXPORT_INDEX, {"created": datetime.now(timezone.utc).isoformat(), "images": entries})
        built = work / tar.name
        with tarfile.open(built, "w") as stream:  # uncompressed: layers are compressed already
            stream.add(layout, arcname=".")
        if to_stdout:
            with built.open("rb") as finished:
                shutil.copyfileobj(finished, sys.stdout.buffer, 8 * 1024 * 1024)
            sys.stdout.buffer.flush()
            progress(f"wrote {len(entries)} image tag(s), {built.stat().st_size} bytes, to stdout")
            return
        checksum = sha256(built)
        sidecar = work / (tar.name + ".sha256")
        sidecar.write_text(f"{checksum}  {tar.name}\n")
        land(tar, lambda hidden: shutil.move(built, hidden))
        land(tar.with_name(sidecar.name), lambda hidden: shutil.move(sidecar, hidden))
    progress(f"wrote {tar} and {tar.name}.sha256 ({len(entries)} image tag(s), {tar.stat().st_size} bytes)")


def checked(tar):
    """tar's sha256, after checking it against the .sha256 beside it."""
    sidecar = tar.with_name(tar.name + ".sha256")
    if not sidecar.exists():
        raise MirrorError(f"{sidecar.name} is missing; it must travel with {tar.name} "
                          "(import --no-checksum imports a tar without it)")
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
    raise MirrorError(f"{tar.name} holds no {EXPORT_INDEX}; is it an export from this program?")


def s3_settings(bucket=None):
    """The bucket send and import use: --s3-bucket, at MIRROR_S3_ENDPOINT with the AWS_* keys."""
    settings = {"endpoint": os.environ.get("MIRROR_S3_ENDPOINT", "").rstrip("/"), "bucket": bucket or "",
                "access": os.environ.get("AWS_ACCESS_KEY_ID", ""), "secret": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
                "region": os.environ.get("AWS_REGION") or "us-east-1", "prefix": os.environ.get("MIRROR_S3_PREFIX", "").strip("/")}
    if not all(settings[k] for k in ("endpoint", "bucket", "access", "secret")):
        raise MirrorError("S3 needs MIRROR_S3_ENDPOINT, a bucket (--s3-bucket), AWS_ACCESS_KEY_ID and "
                          "AWS_SECRET_ACCESS_KEY")
    return settings


def s3_request(s3, method, key="", data=None, length=None, query=None, extra=None):
    """One path-style request to an S3-compatible store, signed with AWS Signature Version 4. A file-like
    data needs its length, or urllib would send it chunked, which S3 refuses. extra headers are signed too."""
    path = f"/{urllib.parse.quote(s3['bucket'])}" + (f"/{urllib.parse.quote(key)}" if key else "")
    canonical_query = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                               for k, v in sorted((query or {}).items()))
    url = s3["endpoint"] + path + (f"?{canonical_query}" if canonical_query else "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {"host": urllib.parse.urlsplit(url).netloc, "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
               "x-amz-date": stamp, **{name.lower(): value for name, value in (extra or {}).items()}}
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
    context = tls_context(env_flag("MIRROR_S3_TLS_VERIFY", True))
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
    """Every key in the bucket under MIRROR_S3_PREFIX."""
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


def attributes(path, checksum):
    """What each delivered file carries, names case for case: the HTTP headers to NiFi, the FlowFile
    attributes in a .ffv3, and S3 object metadata (which an S3 store may lowercase; a .ffv3 in the bucket
    keeps the case). Inside GitLab CI it adds X-CI-Project, X-CI-Pipeline-Url and X-CI-Commit-Sha: for
    routing and display only, a receiver trusts the file, not these."""
    return {"Filename": path.name, "Content-Type": "application/octet-stream", "X-Sha256": checksum,
            "X-Artifact-Type": "container-images", "X-Artifact-Format": "tar", **ci_attributes()}


CI_ATTRIBUTES = {"X-CI-Project": "CI_PROJECT_PATH", "X-CI-Pipeline-Url": "CI_PIPELINE_URL",
                 "X-CI-Commit-Sha": "CI_COMMIT_SHA"}


def header_value(value):
    """A value safe in an HTTP header, a FlowFile attribute or S3 metadata: CR and LF removed."""
    return re.sub(r"[\r\n]", "", str(value)).strip()


def ci_attributes():
    """The CI_* variables that are set and non-empty, as attributes; none outside GitLab CI."""
    found = {name: header_value(os.environ.get(variable, "")) for name, variable in CI_ATTRIBUTES.items()}
    return {name: value for name, value in found.items() if value}


def post(path, headers, url, trust, limits=("--max-time", "3600", "--retry", "10"), token=None):
    """POST one file to url, streamed, with headers (a dict). The one HTTP client for send and the webhook."""
    parts = urllib.parse.urlsplit(url)
    shown = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()  # no credentials in the log
    # HTTP/1.1 keeps the header names as written; HTTP/2 would lowercase them and break a case-sensitive
    # RouteOnAttribute. --upload-file streams the file (--data-binary would read it all into memory),
    # an empty Expect skips the 100-continue round trip. NiFi answers 503 while its queue is full
    # (back pressure); ten retries back off from 1 s to about 8 minutes.
    lines = ["Expect:", *(f"{name}: {value}" for name, value in headers.items())]
    with tempfile.TemporaryDirectory(prefix="mirror-post-") as scratch:
        if token:  # read from a 0600 file, so neither ps nor -v shows it
            secret = Path(scratch) / "auth"
            secret.touch(mode=0o600)
            secret.write_text(f"Authorization: Bearer {token}\n")
            lines.append(f"@{secret}")
        status = run("curl", "--http1.1", *trust, "--fail", "--show-error", "--silent", "--connect-timeout", "15",
                     *limits, "--request", "POST", "--output", "/dev/null", "--write-out", "%{http_code}",
                     *[item for header in lines for item in ("--header", header)], "--upload-file", str(path), url)
    progress(f"posted to NiFi: {path.name} -> {shown} (HTTP {status.decode().strip()}, {path.stat().st_size} bytes)")


def post_file(path, checksum, url):
    """POST one file to a NiFi ListenHTTP, with its attributes as headers."""
    post(path, attributes(path, checksum), url, ca_files()[2] if ca_files() else [],
         ("--max-time", str(max(3600, path.stat().st_size // 1_000_000)), "--retry", "10"))


def notify(low, images):
    """POST once to MIRROR_REQUEST_URL as a send delivery would arrive (the same attribute headers,
    X-Artifact-Type container-images-request) so the low NiFi routes it on attributes, plus X-Registry and
    X-Image-Count (the image list stays in the body: a header is capped near 8 KB). Body: JSON {"registry",
    "images": [{repository, tag, digest}], "references": "repo:tag" lines}. Nothing when unset."""
    url = os.environ.get("MIRROR_REQUEST_URL", "").strip()
    if not url or not images:
        return
    body = {"registry": low, "images": images,
            "references": "\n".join(f"{i['repository']}:{i['tag']}" for i in images)}
    with tempfile.TemporaryDirectory(prefix="mirror-webhook-") as scratch:
        payload = Path(scratch) / "container-images-request.json"
        payload.write_text(json.dumps(body))
        headers = {**attributes(payload, sha256(payload)), "Content-Type": "application/json",
                   "X-Artifact-Type": "container-images-request", "X-Artifact-Format": "json",
                   "X-Registry": header_value(low), "X-Image-Count": str(len(images))}
        bundle = ca_files("MIRROR_REQUEST_CA_BUNDLE") or ca_files()
        post(payload, headers, url, bundle[2] if bundle else [], ("--max-time", "30", "--retry", "2"),
             os.environ.get("MIRROR_REQUEST_TOKEN", "").strip() or None)


def request(path, ledger_file, entries=(), files_from=None, everything=False, dry_run=False):
    """Ask the low NiFi, through MIRROR_REQUEST_URL, to carry images in MIRROR_REGISTRY: the named ones,
    else every synced catalog image (everything), else those synced and not yet requested at that digest.
    The ledger records them as requested once the POST succeeds."""
    if not os.environ.get("MIRROR_REQUEST_URL", "").strip():
        raise MirrorError("request needs MIRROR_REQUEST_URL")
    low = registry("MIRROR_REGISTRY")
    images = []
    # ponytail: one manifest read per tag; read the ledger's digest instead if a large catalog is too slow.
    for host, repository, tag in dict.fromkeys(wanted_images(path, ledger_file, entries, files_from, everything,
                                                             "requested")):
        if host != low:
            raise MirrorError(f"{host}/{repository}:{tag}: the webhook names images in MIRROR_REGISTRY ({low}) only")
        digest = present_digest(f"docker://{low}/{repository}:{tag}", "MIRROR_REGISTRY")
        if not digest:  # every name is checked before the post, so a typo sends nothing
            raise MirrorError(f"{low}/{repository}:{tag} is not in MIRROR_REGISTRY; check the name, nothing was requested")
        images.append({"repository": repository, "tag": tag, "digest": digest})
    if not images:
        progress("nothing new to request")
        return
    if dry_run:
        for image in images:
            print(f"would request: {low}/{image['repository']}:{image['tag']}@{image['digest']}")
        return
    notify(low, images)
    ledger = load_ledger(ledger_file, low)
    ledger["requested"].update({f"{i['repository']}:{i['tag']}": i["digest"] for i in images})
    save_ledger(ledger_file, ledger)
    progress(f"requested {len(images)} image tag(s) from {low}")


def land(destination, write):
    """Have write fill a hidden .NAME.partial, then rename it to destination in one step on the same
    filesystem (NFS included). A directory watcher never sees half a file: NiFi ListFile and GetFile skip
    dot files by default, and import reads only NAME.tar and NAME.tar.ffv3."""
    hidden = destination.with_name(f".{destination.name}.partial")
    try:
        write(hidden)
    except BaseException:
        hidden.unlink(missing_ok=True)  # a full disk or a dropped share leaves nothing behind
        raise
    os.replace(hidden, destination)


FLOWFILE_MAGIC = b"NiFiFF3"
# A name a FlowFile may carry and import writes: no path, not hidden, short enough for any filesystem.
SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,199}")


def write_flowfiles(target, files):
    """A NiFi FlowFile v3 stream (UnpackContent, flowfile-stream-v3): each (path, attributes) in turn."""
    def field(length):
        return length.to_bytes(2, "big") if length < 0xFFFF else b"\xff\xff" + length.to_bytes(4, "big")

    with target.open("wb") as stream:
        for path, attrs in files:
            stream.write(FLOWFILE_MAGIC + field(len(attrs)))
            for text in (item for pair in attrs.items() for item in pair):
                stream.write(field(len(text.encode())) + text.encode())
            stream.write(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as content:
                shutil.copyfileobj(content, stream, 8 * 1024 * 1024)


def read_flowfiles(source, directory):
    """Unpack a FlowFile v3 stream into directory, each content under its filename attribute:
    [(path, attributes)]."""
    def exact(stream, size):
        data = stream.read(size)
        if len(data) != size:
            raise MirrorError(f"{source.name}: truncated FlowFile package")
        return data

    def field(stream):
        length = int.from_bytes(exact(stream, 2), "big")
        return int.from_bytes(exact(stream, 4), "big") if length == 0xFFFF else length

    def text(stream):  # a package from the far side is untrusted: bound what it makes us hold in memory
        length = field(stream)
        if length > 65536:
            raise MirrorError(f"{source.name}: attribute of {length} bytes")
        try:
            return exact(stream, length).decode()
        except UnicodeDecodeError as error:
            raise MirrorError(f"{source.name}: attribute is not UTF-8") from error

    written = []
    with source.open("rb") as stream:
        while magic := stream.read(len(FLOWFILE_MAGIC)):
            if magic != FLOWFILE_MAGIC:
                raise MirrorError(f"{source.name} is not a NiFi FlowFile v3 package")
            count = field(stream)
            if count > 1000:
                raise MirrorError(f"{source.name}: {count} attributes")
            attrs = {}
            for _ in range(count):
                key = text(stream)
                attrs[key] = text(stream)
            name = attrs.get("filename", "")
            if not SAFE_NAME.fullmatch(name):
                raise MirrorError(f"{source.name}: unsafe filename attribute {name[:80]!r}")
            remaining = int.from_bytes(exact(stream, 8), "big")
            with (directory / name).open("wb") as out:
                while remaining:
                    chunk = exact(stream, min(remaining, 8 * 1024 * 1024))
                    out.write(chunk)
                    remaining -= len(chunk)
            written.append((directory / name, attrs))
    return written


def deliver(path, attrs, nifi_url, s3, directory):
    if nifi_url:
        post_file(path, attrs["X-Sha256"], nifi_url)
    elif s3:
        metadata = {f"x-amz-meta-{name}": value for name, value in attrs.items() if name != "Content-Type"}
        with path.open("rb") as stream:
            s3_request(s3, "PUT", s3_key(s3, path.name), stream, path.stat().st_size,
                       extra={"Content-Type": attrs["Content-Type"], **metadata}).close()
        progress(f"uploaded: {path.name} -> s3 {s3['bucket']}/{s3_key(s3, path.name)}")
    else:
        land(Path(directory) / path.name, lambda hidden: shutil.copyfile(path, hidden))
        progress(f"copied: {path.name} -> {directory}")


def send(tars, ledger_file, nifi_url=None, s3_bucket=None, directory=None, flowfile=False, dry_run=False):
    """Deliver each tar, then its .sha256, to one place: a NiFi ListenHTTP, an S3 bucket or a directory
    (an NFS share, a USB stick). With flowfile, a directory or a bucket gets one NAME.tar.ffv3 instead: the
    tar with its attributes, X-Sha256 among them, so a link that delivers files out of order or hours apart
    has one file to carry. Once a tar is delivered its images count as sent in the ledger. With dry_run the
    tars are checked and the deliveries printed, nothing is delivered or recorded."""
    if sum(bool(where) for where in (nifi_url, s3_bucket, directory)) != 1:
        raise MirrorError("send needs exactly one of --nifi-url, --s3-bucket or --target-directory")
    s3 = s3_settings(s3_bucket) if s3_bucket else None
    if directory and not Path(directory).is_dir():  # a share that is not mounted must not turn into a local directory
        raise MirrorError(f"{directory} does not exist; mount or create it first")
    digests = {Path(tar): checked(Path(tar)) for tar in tars}  # every tar is checked before the first is sent
    if flowfile and nifi_url:
        raise MirrorError("--flowfile goes with --target-directory or --s3-bucket; NiFi gets the attributes as headers")
    for tar in digests:  # the far side writes files by these names; refuse here what it would refuse there
        if not SAFE_NAME.fullmatch(tar.name + ".sha256"):
            raise MirrorError(f"{tar.name}: use letters, digits, '.', '_' and '-' only, under 190 characters")
    for tar, digest in digests.items():
        if dry_run:
            where = (re.sub(r"://[^/@\s]+@", "://", nifi_url) if nifi_url
                     else f"s3 {s3['bucket']}/{s3_key(s3, '')}".rstrip("/") if s3 else directory)
            print(f"would send: {tar.name}" + ("" if flowfile else f" and {tar.name}.sha256")
                  + (f" as {tar.name}.ffv3" if flowfile else "") + f" -> {where}")
            continue
        sidecar = tar.with_name(tar.name + ".sha256")
        files = [(tar, attributes(tar, digest)), (sidecar, attributes(sidecar, sha256(sidecar)))]
        if not flowfile:
            for path, attrs in files:  # the .sha256 last: a tar is complete once its checksum is there
                deliver(path, attrs, nifi_url, s3, directory)
        else:
            # Also the lowercase core filename attribute: UnpackContent and PutFile name the file by it.
            packed = [(tar, {"filename": tar.name, **files[0][1]})]
            with staging_dir(tar.parent) if s3 else nullcontext(Path(directory)) as where:
                package = where / (tar.name + ".ffv3")
                land(package, lambda hidden: write_flowfiles(hidden, packed))
                progress(f"packaged: {tar.name} with X-Sha256 {digest} as {package}")
                if s3:
                    deliver(package, {**attributes(package, sha256(package)), "X-Artifact-Format": "ffv3"},
                            None, s3, None)
        record_sent(tar, ledger_file)
        progress(f"sent: {tar.name}")


def record_sent(tar, ledger_file):
    """Mark the images of a delivered tar as sent, when the ledger belongs to the registry they came from."""
    index = read_index(tar)
    low = os.environ.get("MIRROR_REGISTRY", "").strip().rstrip("/")
    ledger = load_ledger(ledger_file, low, create=False) if low else None
    if ledger is None:
        progress(f"not recorded: no ledger for {low or 'MIRROR_REGISTRY (unset)'}")
        return
    hosts = [image.get("source", "").split("/", 1)[0] for image in index["images"]]
    sent = {image["source"].split("/", 1)[1]: image["digest"] for image, host in zip(index["images"], hosts) if host == low}
    if sent:
        ledger["sent"].update(sent)
        if save_ledger(ledger_file, ledger):
            progress(f"ledger: {len(sent)} image tag(s) recorded as sent")
        else:
            progress("ledger: read only, the pipeline's ledger is unchanged")


def check_entry(image):
    """Refuse an export.json entry that is not a numbered dir, a repository path, a tag and a digest."""
    if (not isinstance(image, dict) or not re.fullmatch(r"[0-9]{6}", str(image.get("dir")))
            or not REPOSITORY.fullmatch(str(image.get("target"))) or not re.fullmatch(TAG, str(image.get("tag")))
            or not re.fullmatch(DIGEST, str(image.get("digest")))):
        raise MirrorError(f"invalid entry in {EXPORT_INDEX}: {image!r}")


def plan_import(tar, high, no_clobber, base):
    """What importing tar would do, printed: push, move a tag, or nothing. Reads the registry only."""
    index = read_index(tar)
    plan = []
    for image in index["images"]:
        check_entry(image)
        target = rebase(image["target"], base)
        present = present_digest(f"docker://{high}/{target}:{image['tag']}", "MIRROR_REGISTRY")
        if present and present != image["digest"] and no_clobber:
            raise MirrorError(f"{high}/{target}:{image['tag']} is {present}, the tar has {image['digest']}; "
                              "--no-clobber does not move a tag")
        plan.append((image, target, present))
    for image, target, present in plan:
        reference = f"{high}/{target}:{image['tag']}"
        if present == image["digest"]:
            print(f"already there: {reference}@{present}")
        elif present:
            print(f"would move tag: {reference} from {present} to {image['digest']}")
        else:
            print(f"would push: {reference}@{image['digest']}")
    progress(f"dry run: {tar.name} holds {len(plan)} image tag(s) for {high}, nothing changed")


def import_tar(tar, staging=None, no_clobber=False, base=None, record=None, no_checksum=False, dry_run=False):
    """Push every image in an export tar into MIRROR_REGISTRY, tags and digests unchanged. Every image is
    checked before the first push, so a refusal pushes nothing. A tag already at its digest is skipped.
    With no_checksum a tar whose .sha256 is missing still imports: each image is still checked against
    its digest in export.json, and the registry checks every layer. With dry_run it checks the .sha256 and
    export.json, reads the registry and prints what would be pushed, without unpacking or pushing."""
    high = registry("MIRROR_REGISTRY")
    if no_checksum and not tar.with_name(tar.name + ".sha256").exists():
        warn(f"{tar.name}.sha256 is missing; importing {tar.name} on its image digests alone")
    else:
        checked(tar)
    if dry_run:
        return plan_import(tar, high, no_clobber, base)
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
            check_entry(image)
            directory = layout / image["dir"]
            if "sha256:" + sha256(directory / "manifest.json") != image["digest"]:
                raise MirrorError(f"{image['target']}:{image['tag']} does not match its recorded digest")
            target = rebase(image["target"], base)
            destination = f"docker://{high}/{target}:{image['tag']}"
            present = present_digest(destination, "MIRROR_REGISTRY")
            if present and present != image["digest"] and no_clobber:
                raise MirrorError(f"{high}/{target}:{image['tag']} is {present}, the tar has {image['digest']}; "
                                  "--no-clobber does not move a tag")
            plan.append((image, directory, target, destination, present))
        for image, directory, target, destination, present in plan:
            if present == image["digest"]:
                progress(f"already there: {high}/{target}:{image['tag']}@{present}")
            else:
                if present:  # as docker push does: the tag moves, the old image stays reachable by digest
                    progress(f"moving tag: {high}/{target}:{image['tag']} from {present} to {image['digest']}")
                copy(f"dir:{directory}", destination, None, "MIRROR_REGISTRY")
                if raw_digest(destination, "MIRROR_REGISTRY") != image["digest"]:
                    raise MirrorError(f"digest mismatch in MIRROR_REGISTRY after the push: {target}:{image['tag']}")
                progress(f"imported: {high}/{target}:{image['tag']}@{image['digest']}")
            if record:
                entries = json.loads(record.read_text()) if record.exists() else []
                record.write_text(json.dumps(entries + [{"target": target, "tag": image["tag"],
                                                         "digest": image["digest"]}], indent=2) + "\n")
    progress(f"{tar.name}: {len(plan)} image tag(s) in {high}, digests verified")


def import_package(package, staging=None, no_clobber=False, base=None, record=None, dry_run=False):
    """Import the tar inside a .ffv3 that send --flowfile (or NiFi PackageFlowFile) wrote, checked against
    its X-Sha256 attribute."""
    with staging_dir(package.parent, staging) as unpacked:
        tars = [(path, attrs) for path, attrs in read_flowfiles(package, unpacked) if path.suffix == ".tar"]
        if len(tars) != 1:
            raise MirrorError(f"{package.name} holds {len(tars)} tars, not one")
        tar, attrs = tars[0]
        if not re.fullmatch(r"[0-9a-f]{64}", attrs.get("X-Sha256", "")):
            raise MirrorError(f"{package.name}: {tar.name} has no X-Sha256 attribute")
        tar.with_name(tar.name + ".sha256").write_text(f"{attrs['X-Sha256']}  {tar.name}\n")  # checked by import_tar
        import_tar(tar, unpacked, no_clobber, base, record, dry_run=dry_run)


def import_all(sources, s3_bucket=None, staging=None, no_clobber=False, base=None, record=None, keep=False,
               no_checksum=False, dry_run=False):
    """Import tar or .ffv3 files, every one in a directory, or every one in an S3 bucket. A directory or a
    bucket is an inbox: each file is removed once imported, unless keep, and one that fails stays put
    without holding up the rest. A file named on its own is kept. With dry_run each file is checked and the
    plan printed, with a would remove: line for every inbox file, and nothing is pushed, removed or recorded
    (a bucket's files are listed, not downloaded)."""
    done, failed = 0, []

    def attempt(name, work, remove):
        nonlocal done
        try:
            work()
        except MirrorError as error:
            fail(f"{name}: {error}; left in place")
            failed.append(name)
            return
        done += 1
        if keep:
            return
        if dry_run:
            print(f"would remove: {name}")
        else:
            remove()
            progress(f"removed: {name}")

    def fetch(s3, key, local):
        with s3_request(s3, "GET", key) as response, local.open("wb") as stream:
            shutil.copyfileobj(response, stream, 8 * 1024 * 1024)

    def from_s3(s3, key, sidecar):
        if dry_run:
            print(f"would import: s3 {s3['bucket']}/{key}")
            return
        with staging_dir(".", staging) as download:
            local = download / key.rsplit("/", 1)[-1]
            fetch(s3, key, local)
            if sidecar:
                fetch(s3, key + ".sha256", local.with_name(local.name + ".sha256"))
                import_tar(local, download, no_clobber, base, record)
            else:
                import_package(local, download, no_clobber, base, record)

    for source in map(Path, sources):
        if source.is_dir():
            for package in sorted(source.glob("*.ffv3")):
                attempt(f"{source}/{package.name}", lambda: import_package(package, staging, no_clobber, base, record, dry_run),
                        package.unlink)
            for tar in sorted(source.glob("*.tar")):
                sidecar = tar.with_name(tar.name + ".sha256")
                if not sidecar.exists() and not no_checksum:
                    progress(f"waiting for {sidecar.name}; skipped (--no-checksum imports it now)")
                    continue
                # The tar first: a .sha256 left alone is never listed.
                attempt(f"{source}/{tar.name}", lambda: import_tar(tar, staging, no_clobber, base, record, no_checksum, dry_run),
                        lambda: (tar.unlink(), sidecar.unlink(missing_ok=True)))
        elif source.suffix == ".ffv3":
            import_package(source, staging, no_clobber, base, record, dry_run)
            done += 1
        else:
            import_tar(source, staging, no_clobber, base, record, no_checksum, dry_run)
            done += 1
    if s3_bucket:
        s3 = s3_settings(s3_bucket)
        keys = set(s3_list(s3))
        for key in sorted(k for k in keys if k.endswith(".ffv3")):
            attempt(f"s3 {s3['bucket']}/{key}", lambda: from_s3(s3, key, False),
                    lambda: s3_request(s3, "DELETE", key).close())
        for key in sorted(k for k in keys if k.endswith(".tar")):
            if key + ".sha256" not in keys:
                progress(f"waiting for {key}.sha256; skipped")
                continue
            attempt(f"s3 {s3['bucket']}/{key}", lambda: from_s3(s3, key, True),
                    lambda: [s3_request(s3, "DELETE", name).close() for name in (key, key + ".sha256")])
    progress(f"dry run: {done} file(s) would be imported, nothing changed" if dry_run and done
             else f"imported {done} file(s)" if done else "nothing to import")
    if failed:
        raise MirrorError(f"{len(failed)} could not be imported and are left in place: {', '.join(failed)}")


def promote(record, base=None):
    """Copy the images an import recorded from MIRROR_SOURCE_REGISTRY (dev) to MIRROR_REGISTRY (prod), by digest.

    The digest is checked in dev before the copy and in prod after it, so prod holds the bytes
    that were imported and approved, whatever the dev tags point at by now.
    """
    dev, prod = registry("MIRROR_SOURCE_REGISTRY"), registry("MIRROR_REGISTRY")
    if dev == prod:
        raise MirrorError("MIRROR_SOURCE_REGISTRY and MIRROR_REGISTRY are the same registry; nothing to promote")
    images = {}
    for entry in json.loads(record.read_text()):
        if (not isinstance(entry, dict) or not REPOSITORY.fullmatch(str(entry.get("target")))
                or not re.fullmatch(TAG, str(entry.get("tag"))) or not re.fullmatch(DIGEST, str(entry.get("digest")))):
            raise MirrorError(f"{record}: invalid entry {entry!r}")
        images[f"{entry['target']}:{entry['tag']}"] = entry  # a later import's digest for the same tag wins
    if not images:
        progress(f"nothing to promote: {record} lists no images")
        return
    for entry in images.values():
        source = f"docker://{dev}/{entry['target']}@{entry['digest']}"
        if raw_digest(source, "MIRROR_SOURCE_REGISTRY") != entry["digest"]:
            raise MirrorError(f"digest mismatch in MIRROR_SOURCE_REGISTRY: {dev}/{entry['target']}@{entry['digest']}")
        key = f"{rebase(entry['target'], base)}:{entry['tag']}"
        destination = f"docker://{prod}/{key}"
        copy(source, destination, "MIRROR_SOURCE_REGISTRY", "MIRROR_REGISTRY")
        if raw_digest(destination, "MIRROR_REGISTRY") != entry["digest"]:
            raise MirrorError(f"digest mismatch in MIRROR_REGISTRY after the copy: {prod}/{key}")
        progress(f"promoted: {dev}/{entry['target']}@{entry['digest']} -> {prod}/{key}")
    progress(f"promoted {len(images)} image tag(s) from {dev} to {prod}, digests verified")


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


def check_catalog(path, files):
    """Fail when a referenced image is not in the catalog, by source or target name."""
    names, targets, digests = set(), set(), set()
    for image in catalog(path):
        names.add(image["source"].split("@")[0])
        targets.add(f"{image['target']}:{image['tag']}")
        digests.add(image["digest"])
    missing = 0
    for reference, where in references(files):
        if "$" in reference:
            warn(f"skipped: {reference} ({where}) is built from a variable")
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
        raise MirrorError(f"{missing} image(s) not in the mirror catalog; add them with {prog()} add")
    progress("every image is in the mirror catalog")


def skopeo_login(host, side, user, password):
    """skopeo login to host as user. password is bytes sent on stdin; None leaves skopeo to ask on the terminal."""
    verify = env_flag(f"{side}_TLS_VERIFY", True) if side else True
    tls = "true" if verify else "false"
    trust = options(None, reference=host) if verify else []
    command = ["skopeo", "login", f"--tls-verify={tls}", *trust, "--username", user, *(["--password-stdin"] if password is not None else []), host]
    result = subprocess.run(command, input=password, capture_output=password is not None, check=False)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()[-300:] if password is not None else ""
        raise MirrorError(f"skopeo login {host} failed" + (f": {detail}" if detail else ""))
    progress(f"logged in to {host} as {user}")


def login(host=None, user=None, password_stdin=False):
    """skopeo login, like skopeo login itself: to host as user, the password on stdin with password_stdin.

    Without arguments it logs in to MIRROR_REGISTRY, and to MIRROR_SOURCE_REGISTRY, each when its
    _USERNAME is set (the password is its _PASSWORD). A registry without authentication needs no login.
    With a host or a user it logs in to that one registry, host defaulting to MIRROR_REGISTRY, the user and
    password to that registry's _USERNAME and _PASSWORD.
    """
    if host is None and user is None and not password_stdin:
        for side in ("MIRROR_REGISTRY", "MIRROR_SOURCE_REGISTRY"):
            if os.environ.get(f"{side}_USERNAME"):
                skopeo_login(registry(side), side, os.environ[f"{side}_USERNAME"],
                             os.environ.get(f"{side}_PASSWORD", "").encode())
        return
    host = (host or required_env("MIRROR_REGISTRY")).rstrip("/")
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", host):
        raise UsageError(f"{host} is not a registry host[:port], without scheme or path")
    side = side_of(host)
    user = user or (os.environ.get(f"{side}_USERNAME") if side else None)
    if not user:
        raise UsageError(f"login to {host} needs -u USER" + (f" or {side}_USERNAME" if side else ""))
    if password_stdin:
        password = sys.stdin.buffer.read().rstrip(b"\r\n")
    else:
        stored = os.environ.get(f"{side}_PASSWORD") if side else None
        password = stored.encode() if stored else None
    skopeo_login(host, side, user, password)


EXAMPLES = """
Low side:
  export MIRROR_REGISTRY=registry.low.example.com
  {prog} sync                                    # copy images.txt into MIRROR_REGISTRY
  {prog} export                                  # what sync added and send has not delivered, as one tar
  {prog} send container-images-*.tar -t /mnt/transfer           # or --nifi-url URL, or --s3-bucket NAME
  {prog} send container-images-*.tar -t /mnt/transfer --flowfile   # one .ffv3 with the NiFi attributes

High side:
  export MIRROR_REGISTRY=registry.high.example.com
  {prog} import /mnt/diode/received              # every tar there, removed once imported
  {prog} import --s3-bucket received             # every tar in the bucket, removed once imported
  {prog} import /media/usb/carry.tar             # one tar, kept (--no-checksum without its .sha256)

Run "{prog} <command> -h" for each command's options and examples.
"""


def build_parser():
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, epilog=EXAMPLES.replace("{prog}", prog()), formatter_class=raw)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--catalog", type=Path, default=ROOT / "images.txt", help="image catalog (default images.txt)")
    parser.add_argument("--ledger", help="ledger file, or the https URL of the pipeline's ledger to read without "
                                         "changing it (or MIRROR_LEDGER; default the package registry in CI, "
                                         "else .mirror-ledger.json beside the catalog)")
    parser.add_argument("--registry", metavar="HOST",
                        help="this side's registry, host[:port] (or MIRROR_REGISTRY): the low one for sync, export and "
                             "send, the high one for import, the destination for promote")
    parser.add_argument("--source-registry", metavar="HOST",
                        help="the registry promote copies from, and one more registry login stores credentials for "
                             "(or MIRROR_SOURCE_REGISTRY)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every skopeo and curl command (or MIRROR_VERBOSE=true)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print no progress on stderr; warnings, errors and data stay")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def command(name, summary, examples, about=""):
        return commands.add_parser(name, help=summary, description=summary, epilog=(about + "examples:\n" + examples).replace("{prog}", prog()),
                                   formatter_class=raw)

    def staging_flag(sub):
        sub.add_argument("--staging-dir", type=Path,
                         help="where the working copy goes, for a bigger disk (or MIRROR_STAGING_DIR; default beside "
                              "the tar). Always removed afterwards")

    def dry_flag(sub, does):
        sub.add_argument("-n", "--dry-run", action="store_true",
                         help=f"print what would happen ({does}) and change nothing")

    def base_flag(sub):
        group = sub.add_mutually_exclusive_group()
        group.add_argument("--rewrite-base-path", metavar="ORG", default=os.environ.get("MIRROR_REWRITE_BASE_PATH"),
                           help="replace the first path segment of every image: team/app -> ORG/app "
                                "(or MIRROR_REWRITE_BASE_PATH)")
        group.add_argument("--strip-components", type=strip_count, metavar="N",
                           default=os.environ.get("MIRROR_STRIP_COMPONENTS") or None,
                           help="remove the first N path segments of every image: with 1, team/prom/prometheus -> "
                                "prom/prometheus (or MIRROR_STRIP_COMPONENTS=N)")

    sync_command = command("sync", "copy every catalog image into MIRROR_REGISTRY; safe to run again",
                           "  {prog} sync\n  {prog} sync --dry-run                             # which tags it would copy",
                           "--dry-run reads both registries and prints a would copy: line per tag; it copies nothing,\n"
                           "saves no ledger and posts no webhook.\n\n")
    dry_flag(sync_command, "tags to copy")
    export_command = command(
        "export", "copy images from a registry into a tar, with a .sha256 beside it",
        "  {prog} export                                  # what is new since the last send\n"
        "  {prog} export --all                            # every catalog image\n"
        "  {prog} export team/prometheus:v3.13.4 team/grafana   # in MIRROR_REGISTRY: a tag, every tag\n"
        "  {prog} export registry.low.example.com/team/app:v1.2.3\n"
        "  {prog} export -T carry.txt -o /share/carry.tar    # one reference per line, - reads stdin\n"
        "  {prog} export --max-size 4GiB                  # several tars, none over 4 GiB unless one image is\n"
        "  {prog} export --rewrite-base-path new-org-dev  # team/app lands as new-org-dev/app\n"
        "  {prog} export --strip-components 1             # team/prom/prometheus lands as prom/prometheus\n"
        "  {prog} export --dry-run                           # the images and tar names; nothing is written\n"
        "  {prog} export -o - | tee carry.tar | sha256sum    # the tar on stdout, messages on stderr",
        "which images:\n"
        "  no names       catalog images sync put in MIRROR_REGISTRY that send has not delivered at that digest\n"
        "  --all          every catalog image sync has put in MIRROR_REGISTRY\n"
        "  names, -T      exactly those references; repo:tag reads MIRROR_REGISTRY, host/repo:tag that host,\n"
        "                 repo with no tag every tag; the catalog and the ledger are not consulted\n"
        "each image is copied whole by digest (every platform, as the registry holds it). export only reads\n"
        "registries and never changes the ledger: send records what was delivered.\n\n"
        "what it writes:\n"
        "  NAME.tar        uncompressed: export.json (each image's source, target path, tag, digest) and one\n"
        "                  skopeo dir: copy per image (000001/, 000002/, ...), shared layers stored once\n"
        "  NAME.tar.sha256 written after the tar; both land under a hidden name and are renamed when complete\n"
        "  -o -            one tar on stdout, no .sha256, not with --max-size or --dry-run; a terminal is refused\n"
        "  --dry-run       prints would write: lines with the images of each tar; no tar, no staging directory\n"
        "  --max-size      NAME-001-of-00N.tar and so on, by the sizes the manifests declare; an image over\n"
        "                  the cap goes in a tar of its own\n"
        "  an existing tar or .sha256 of the same name is refused, never overwritten\n"
        "  the staging directory needs about twice the export's size\n\n"
        "check a tar:\n"
        "  sha256sum -c NAME.tar.sha256\n"
        "  tar -xOf NAME.tar ./export.json | python3 -m json.tool\n\n")
    export_command.add_argument("images", nargs="*", help="registry/repo:tag, or repo:tag in MIRROR_REGISTRY; no tag: every tag")
    export_command.add_argument("-T", "--files-from", type=Path, action="append", metavar="FILE",
                                help="a file of image references, one per line, # comments; - is stdin; repeat for more files")
    export_command.add_argument("--all", action="store_true",
                                help="every catalog image, not only what is new (or MIRROR_RESEND_ALL=true)")
    export_command.add_argument("-o", "--output", type=Path, help="the tar to write (default container-images-<UTC time>.tar here), or - for stdout, "
                                     "with no .sha256: pipe it to tee and sha256sum")
    export_command.add_argument("--max-size", type=size, default=os.environ.get("MIRROR_EXPORT_MAX_SIZE") or None, metavar="SIZE",
                                help="split into tars of at most this size, e.g. 4GiB or 500MB (or MIRROR_EXPORT_MAX_SIZE; "
                                     "K M G T and KiB MiB GiB TiB are powers of 1024, KB MB GB TB of 1000; default one tar)")
    staging_flag(export_command)
    base_flag(export_command)
    dry_flag(export_command, "the images and the tar names")
    send_command = command(
        "send", "deliver tars and their .sha256 files to NiFi, an S3 bucket or a directory",
        "  {prog} send container-images-*.tar -t /mnt/transfer         # an NFS share or a USB stick\n"
        "  {prog} send container-images-*.tar --nifi-url https://nifi.low:9443/contentListener\n"
        "  {prog} send container-images-*.tar --s3-bucket transfer     # MIRROR_S3_ENDPOINT and AWS_* keys\n"
        "  {prog} send container-images-*.tar -t /mnt/transfer --flowfile   # one NAME.tar.ffv3 per tar\n"
        "  {prog} send container-images-*.tar -t /mnt/transfer --dry-run    # checks the tars, delivers nothing",
        "every tar is checked against its .sha256 before the first is sent; then each tar is delivered, its\n"
        ".sha256 last, and its images are marked sent in MIRROR_REGISTRY's ledger. A tar that fails is not\n"
        "marked, so the next plain export takes it again.\n\n"
        "destinations:\n"
        "  -t, --target-directory  a mounted directory; each file is written as .NAME.partial and renamed when complete\n"
        "  --nifi-url  one HTTP/1.1 POST per file to a NiFi ListenHTTP\n"
        "  --s3-bucket path-style bucket at MIRROR_S3_ENDPOINT with the AWS_* keys\n"
        "  --flowfile  with -t or --s3-bucket: one NiFi FlowFile v3 NAME.tar.ffv3 per tar, no separate .sha256\n\n"
        "every file carries Filename, Content-Type, X-Sha256, X-Artifact-Type: container-images and\n"
        "X-Artifact-Format: tar (POST headers, S3 metadata or FlowFile attributes), plus X-CI-Project,\n"
        "X-CI-Pipeline-Url and X-CI-Commit-Sha in CI.\n\n")
    send_command.add_argument("tars", nargs="+", type=Path, help="tar files export wrote")
    where = send_command.add_mutually_exclusive_group(required=True)
    where.add_argument("--nifi-url", help="a NiFi ListenHTTP URL")
    where.add_argument("--s3-bucket", help="a bucket at MIRROR_S3_ENDPOINT (MIRROR_S3_PREFIX optional)")
    where.add_argument("-t", "--target-directory", dest="directory", metavar="DIR", help="a directory; it must exist")
    dry_flag(send_command, "the deliveries")
    send_command.add_argument("--flowfile", action="store_true",
                              default=False,
                              help="with --target-directory or --s3-bucket, deliver one NAME.tar.ffv3 (NiFi FlowFile v3) holding the tar and its "
                                   ".sha256 with their attributes (or MIRROR_SEND_FLOWFILE=true)")
    import_command = command(
        "import", "push the images in export tars, or in an inbox directory or bucket of them, into MIRROR_REGISTRY, tags and digests unchanged",
        "  {prog} import /mnt/diode/received          # every tar there; each removed once imported\n"
        "  {prog} import --s3-bucket received         # every tar in the bucket; each removed once imported\n"
        "  {prog} import /media/usb/carry.tar         # one tar, checked against carry.tar.sha256, kept\n"
        "  {prog} import /mnt/diode/received --rewrite-base-path new-org-dev\n"
        "  {prog} import /mnt/diode/received --strip-components 1   # team/app lands as app\n"
        "  {prog} import /media/usb/carry.tar --no-checksum      # the .sha256 was not copied\n"
        "  {prog} import /mnt/diode/received/container-images-1.tar.ffv3   # a NiFi FlowFile v3 package\n"
        "  {prog} import /mnt/diode/received --dry-run   # the plan, with would remove: for each inbox file",
        "sources:\n"
        "  a tar or .ffv3   imported, then kept\n"
        "  a directory      an inbox: every tar and .ffv3, each removed once imported (-k, --keep leaves it);\n"
        "                   one that fails stays put, a tar without its .sha256 waits\n"
        "  --s3-bucket      the same inbox, in a bucket\n\n"
        "checks: the .sha256 (or a .ffv3's X-Sha256), then every image against its digest in export.json\n"
        "before the first push, so a refusal pushes nothing, then each digest again after the push.\n"
        "tags: one already at its digest is skipped; one at another digest moves, as docker push moves it\n"
        "(--no-clobber refuses the whole tar instead, before any push). Unpacking needs up to twice the tar's size.\n"
        "--dry-run checks each .sha256 and export.json and reads the registry, then prints would push:, would move tag:\n"
        "and would remove: lines. It unpacks nothing, so image contents are checked only by a real import.\n\n")
    import_command.add_argument("sources", nargs="*", type=Path, help="tar files or directories of them")
    import_command.add_argument("--s3-bucket", help="import every tar in this bucket at MIRROR_S3_ENDPOINT (MIRROR_S3_PREFIX optional)")
    import_command.add_argument("-k", "--keep", action="store_true", help="leave imported tars in the inbox directory or bucket")
    import_command.add_argument("--no-clobber", action="store_true",
                                help="refuse to move a tag that already points at another digest; by default a tag "
                                     "moves, as docker push moves it")
    import_command.add_argument("--no-checksum", action="store_true",
                                help="import a tar whose .sha256 is missing, on its image digests alone; "
                                     "a .sha256 that is there is still checked")
    import_command.add_argument("--record", type=Path, help="append the image tags and digests pushed, for promote")
    staging_flag(import_command)
    base_flag(import_command)
    dry_flag(import_command, "the pushes and the inbox files it would remove")

    request_command = command(
        "request", "ask the low NiFi, through MIRROR_REQUEST_URL, to carry images from MIRROR_REGISTRY",
        "  {prog} request                       # synced and not yet requested (sync does this itself)\n"
        "  {prog} request --all                 # every synced catalog image again\n"
        "  {prog} request team/app:v1 team/db   # a tag, every tag of a repository\n"
        "  {prog} request -T images.txt         # images.txt lines work: each names its target:tag\n"
        "  {prog} request -T - --dry-run        # paste lines on stdin, print the request, post nothing",
        "one POST, the same body and headers as the webhook sync sends. Names are references in\n"
        "MIRROR_REGISTRY or images.txt lines; the ledger records what was requested.\n\n")
    request_command.add_argument("images", nargs="*", help="repo:tag or repo in MIRROR_REGISTRY, or an images.txt line")
    request_command.add_argument("-T", "--files-from", type=Path, action="append", metavar="FILE",
                                 help="a file of references or images.txt lines, one per line; - is stdin")
    request_command.add_argument("--all", action="store_true", help="every synced catalog image (or MIRROR_RESEND_ALL=true)")
    dry_flag(request_command, "the images it would request")

    login_command = command(
        "login", "log in to a registry for skopeo, as skopeo login does",
        "  {prog} --registry registry.example.com login -u robot --password-stdin < token.txt\n"
        "  {prog} login docker.io -u user --password-stdin < token.txt\n"
        "  MIRROR_REGISTRY=registry.example.com MIRROR_REGISTRY_USERNAME=robot \\\n"
        "    MIRROR_REGISTRY_PASSWORD=... {prog} login",
        "which registry:\n"
        "  REGISTRY       that registry; with -u or --password-stdin and no REGISTRY, --registry\n"
        "  nothing at all MIRROR_REGISTRY, and MIRROR_SOURCE_REGISTRY, each when its _USERNAME is set,\n"
        "                 with its _PASSWORD. A registry without authentication needs no login\n"
        "the password is never an argument: it comes from --password-stdin, the registry's _PASSWORD, or\n"
        "skopeo asks for it on the terminal.\n\n")
    login_command.add_argument("login_registry", nargs="?", metavar="REGISTRY", help="registry host[:port] to log in to")
    login_command.add_argument("-u", "--username", help="the account (default the registry's _USERNAME variable)")
    login_command.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    add = command("add", "resolve tags to digests and add catalog entries",
                  "  {prog} add docker.io/prom/prometheus:v3.13.4 team/prometheus\n"
                  "  {prog} add docker.io/bitnamicharts/redis:22.0.7 charts/redis   # an OCI Helm chart\n"
                  "  {prog} add -T lists/base.txt -T lists/extra.txt --prefix team   # many images, one per line\n"
                  "  cat list.txt | {prog} add -T - --prefix team",
                  "two forms:\n"
                  "  SOURCE TARGET  one image: registry/repository:tag in full, and its repository path in the registry\n"
                  "  -T FILE        every image a list file names, one per line, # comments, - is stdin, repeatable;\n"
                  "                 a line without a registry host is read as docker pull reads it, without a tag :latest;\n"
                  "                 the target is the source path without its host, under --prefix. What resolves is\n"
                  "                 written even when other lines fail.\n\n")
    add.add_argument("operands", nargs="*", metavar="SOURCE TARGET",
                     help="registry/repository:tag in full, e.g. docker.io/library/alpine:3.20, then the repository "
                          "path in the registry, any depth, e.g. team/prometheus")
    add.add_argument("-T", "--files-from", type=Path, action="append", metavar="FILE",
                     help="a file of image references, one per line, # comments; - is stdin; repeat for more files")
    add.add_argument("--prefix", help="with -T, the repository path the targets go under, e.g. team")
    command("list", "validate and list the image catalog", "  {prog} list")
    command("pending", "list catalog sources sync has not copied yet, one per line", "  {prog} pending")
    check = command("check", "fail when a Containerfile or manifest uses an image the catalog lacks",
                    "  {prog} check Containerfile\n  helm template chart/ | {prog} check -")
    check.add_argument("files", nargs="+", help="Containerfiles, manifests or - for rendered YAML on stdin")
    promote_command = command("promote", "copy recorded images from MIRROR_SOURCE_REGISTRY (dev) to MIRROR_REGISTRY (prod) by digest",
                              "  MIRROR_SOURCE_REGISTRY=registry.dev.example.com MIRROR_REGISTRY=registry.prod.example.com \\\n"
                              "    {prog} promote imported.json      # the file import --record wrote")
    promote_command.add_argument("record", type=Path, help="the file import --record wrote")
    base_flag(promote_command)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "import" and not args.sources and not args.s3_bucket:
        parser.error("import needs a tar, a directory or --s3-bucket")
    if args.command == "export" and str(args.output) == "-":
        if args.max_size:
            parser.error("-o - writes one tar to stdout; it cannot be split by --max-size")
        if args.dry_run:
            parser.error("--dry-run prints its plan on stdout, which -o - keeps for the tar")
        if sys.stdout.isatty():
            parser.error("refusing to write a tar to a terminal; pipe it or give -o FILE")
    if args.command == "add":
        if args.files_from and args.operands:
            parser.error("add takes SOURCE TARGET or -T FILE, not both")
        if not args.files_from and len(args.operands) != 2:
            parser.error("add needs SOURCE TARGET, or -T FILE")
        if args.prefix and not args.files_from:
            parser.error("--prefix goes with -T")
    global QUIET
    QUIET = args.quiet
    if args.ledger:
        os.environ["MIRROR_LEDGER"] = args.ledger
    if args.registry:
        os.environ["MIRROR_REGISTRY"] = args.registry
    if args.source_registry:
        os.environ["MIRROR_SOURCE_REGISTRY"] = args.source_registry
    try:
        if args.verbose or env_flag("MIRROR_VERBOSE"):
            os.environ["MIRROR_VERBOSE"] = "true"
        base = getattr(args, "rewrite_base_path", None)
        if getattr(args, "strip_components", None):
            if base:
                parser.error("--strip-components and --rewrite-base-path (or their MIRROR_ variables) are both set")
            base = args.strip_components
        ca_files("MIRROR_REQUEST_CA_BUNDLE")
        ca_files()  # before any staging directory moves TMPDIR, and so a bad path fails first
        ledger_file = ledger_location(args.catalog)
        if args.command == "sync":
            sync(args.catalog, ledger_file, args.dry_run)
            if os.environ.get("MIRROR_REQUEST_URL", "").strip() and not args.dry_run:
                try:  # what sync synced and no webhook has asked for yet, or everything with MIRROR_RESEND_ALL
                    request(args.catalog, ledger_file, everything=env_flag("MIRROR_RESEND_ALL"))
                except (MirrorError, OSError) as error:
                    warn(f"webhook not delivered: {error}")
        elif args.command == "request":
            request(args.catalog, ledger_file, args.images, args.files_from, args.all or env_flag("MIRROR_RESEND_ALL"),
                    args.dry_run)
        elif args.command == "export":
            export(args.catalog, ledger_file, args.images, args.files_from, args.all or env_flag("MIRROR_RESEND_ALL"), args.output, args.max_size,
                   args.staging_dir, base, args.dry_run)
        elif args.command == "send":
            send(args.tars, ledger_file, args.nifi_url, args.s3_bucket, args.directory,
                 args.flowfile or env_flag("MIRROR_SEND_FLOWFILE"), args.dry_run)
        elif args.command == "import":
            import_all(args.sources, args.s3_bucket, args.staging_dir, args.no_clobber, base,
                       args.record, args.keep, args.no_checksum, args.dry_run)
        elif args.command == "login":
            login(args.login_registry, args.username, args.password_stdin)
        elif args.command == "add":
            if args.files_from:
                add_files(args.catalog, args.files_from, args.prefix)
            else:
                add_image(args.catalog, *args.operands)
        elif args.command == "list":
            for image in catalog(args.catalog):
                print(f"{image['source']} -> {image['target']}:{image['tag']}")
        elif args.command == "pending":
            pending(args.catalog, ledger_file)
        elif args.command == "check":
            check_catalog(args.catalog, args.files)
        else:
            promote(args.record, base)
        return 0
    except UsageError as error:
        parser.error(str(error))
    except (MirrorError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        fail(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
