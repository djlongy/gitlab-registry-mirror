package io.github.djlongy.nifi.registry;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * What a mirror.py export tar holds, read without unpacking the layers: export.json, then for each
 * image its manifest (and, for a multi-platform index, each child manifest) from the skopeo dir:
 * copy it was exported as. A blob shared between images is stored once; later copies are hard
 * links to it.
 */
final class ExportPlan {

    static final String INDEX = "export.json";
    static final Pattern DIR = Pattern.compile("[0-9]{6}");
    static final Pattern TAG = Pattern.compile("[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}");
    static final Pattern DIGEST = Pattern.compile("sha256:[0-9a-f]{64}");
    static final Pattern HEX = Pattern.compile("[0-9a-f]{64}");
    /** An OCI repository name: lowercase path components separated by '/'. */
    static final Pattern REPOSITORY = Pattern.compile(
            "[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*");
    private static final int SMALL_FILE_LIMIT = 16 * 1024 * 1024;
    /** Every manifest and export.json kept in memory together; the tar comes from the far side. */
    private static final long SMALL_TOTAL_LIMIT = 256L * 1024 * 1024;
    private static final Pattern KEPT = Pattern.compile("export\\.json|[0-9]{6}/(?:manifest|[0-9a-f]{64}\\.manifest)\\.json");
    private static final Pattern MEDIA_TYPE = Pattern.compile("[\\w.+-]+/[\\w.+-]+");
    private static final ObjectMapper JSON = new ObjectMapper();

    record Manifest(String digest, byte[] bytes, String mediaType) {
    }

    record Image(String dir, String target, String tag, String digest, Manifest root, List<Manifest> children,
                 Set<String> blobs) {
    }

    final List<Image> images = new ArrayList<>();
    /** Where each blob's bytes are: its first regular (not hard-linked) entry in the tar. */
    final Map<String, String> blobEntries = new HashMap<>();

    private ExportPlan() {
    }

    static String entryName(TarArchiveEntry entry) {
        String name = entry.getName();
        while (name.startsWith("./")) {
            name = name.substring(2);
        }
        return name;
    }

    static ExportPlan read(InputStream content) throws IOException {
        Map<String, byte[]> small = new HashMap<>();
        Map<String, String> links = new HashMap<>();
        Set<String> names = new HashSet<>();
        long kept = 0;
        ExportPlan plan = new ExportPlan();
        TarArchiveInputStream tar = new TarArchiveInputStream(content);
        TarArchiveEntry entry;
        while ((entry = tar.getNextEntry()) != null) {
            String name = entryName(entry);
            names.add(name);
            if (entry.isLink()) {
                links.put(name, stripDot(entry.getLinkName()));
            } else if (entry.isFile()) {
                String file = name.substring(name.lastIndexOf('/') + 1);
                if (HEX.matcher(file).matches()) {
                    plan.blobEntries.putIfAbsent("sha256:" + file, name);
                } else if (KEPT.matcher(name).matches()) {
                    if (entry.getSize() > SMALL_FILE_LIMIT || (kept += entry.getSize()) > SMALL_TOTAL_LIMIT) {
                        throw new IOException(name + " makes the manifests too large to be an export");
                    }
                    small.put(name, tar.readAllBytes());
                }
            }
        }
        byte[] index = small.get(INDEX);
        if (index == null) {
            throw new IOException("no " + INDEX + " in the content: is it a mirror.py export tar?");
        }
        JsonNode images = JSON.readTree(index).get("images");
        if (images == null || !images.isArray()) {
            throw new IOException("invalid " + INDEX);
        }
        for (JsonNode image : images) {
            String dir = text(image, "dir");
            String target = text(image, "target");
            String tag = text(image, "tag");
            String digest = text(image, "digest");
            if (!DIR.matcher(dir).matches() || !REPOSITORY.matcher(target).matches() || !TAG.matcher(tag).matches()
                    || !DIGEST.matcher(digest).matches()) {
                throw new IOException("invalid entry in " + INDEX + ": " + image);
            }
            Manifest root = manifest(dir + "/manifest.json", small, links, null);
            if (!root.digest().equals(digest)) {
                throw new IOException(target + ":" + tag + " does not match its recorded digest " + digest);
            }
            List<Manifest> children = new ArrayList<>();
            Set<String> blobs = new LinkedHashSet<>();
            JsonNode rootJson = JSON.readTree(root.bytes());
            if (rootJson.has("manifests")) {
                for (JsonNode child : rootJson.get("manifests")) {
                    String childDigest = text(child, "digest");
                    if (!DIGEST.matcher(childDigest).matches()) {
                        throw new IOException(target + ":" + tag + ": unsupported child digest " + childDigest);
                    }
                    String path = dir + "/" + childDigest.substring("sha256:".length()) + ".manifest.json";
                    if (!names.contains(path)) {
                        continue;  // a platform the export left out (MIRROR_PLATFORM)
                    }
                    Manifest manifest = manifest(path, small, links, text(child, "mediaType"));
                    if (!manifest.digest().equals(childDigest)) {
                        throw new IOException(path + " does not match its digest in the index");
                    }
                    children.add(manifest);
                    blobs.addAll(blobsOf(manifest, dir, names));
                }
            } else {
                blobs.addAll(blobsOf(root, dir, names));
            }
            plan.images.add(new Image(dir, target, tag, digest, root, children, blobs));
        }
        return plan;
    }

    private static Manifest manifest(String path, Map<String, byte[]> small, Map<String, String> links, String fallbackType)
            throws IOException {
        String real = links.getOrDefault(path, path);
        byte[] bytes = small.get(real);
        if (bytes == null) {
            throw new IOException("missing " + path);
        }
        JsonNode json = JSON.readTree(bytes);
        String mediaType = json.hasNonNull("mediaType") ? json.get("mediaType").asText() : fallbackType;
        if (mediaType == null || !MEDIA_TYPE.matcher(mediaType).matches()) {
            mediaType = json.has("manifests") ? "application/vnd.oci.image.index.v1+json"
                    : "application/vnd.oci.image.manifest.v1+json";
        }
        return new Manifest(Digests.sha256(bytes), bytes, mediaType);
    }

    /** The config and layers of one image manifest that the export holds (a foreign layer is not copied). */
    private static Set<String> blobsOf(Manifest manifest, String dir, Set<String> names) throws IOException {
        Set<String> blobs = new LinkedHashSet<>();
        JsonNode json = JSON.readTree(manifest.bytes());
        List<JsonNode> descriptors = new ArrayList<>();
        if (json.has("config")) {
            descriptors.add(json.get("config"));
        }
        if (json.has("layers")) {
            json.get("layers").forEach(descriptors::add);
        }
        for (JsonNode descriptor : descriptors) {
            String digest = text(descriptor, "digest");
            if (!DIGEST.matcher(digest).matches()) {
                throw new IOException("unsupported digest " + digest);
            }
            if (names.contains(dir + "/" + digest.substring("sha256:".length()))) {
                blobs.add(digest);
            }
        }
        return blobs;
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value == null ? "" : value.asText();
    }

    private static String stripDot(String name) {
        while (name.startsWith("./")) {
            name = name.substring(2);
        }
        return name;
    }

    /** target with its first path segment replaced (rewrite) or removed (strip). */
    static String rebase(String target, String rewrite, boolean strip) throws IOException {
        String moved = target;
        if (strip) {
            int slash = target.indexOf('/');
            if (slash < 0) {
                throw new IOException(target + " has no base path to strip");
            }
            moved = target.substring(slash + 1);
        } else if (rewrite != null && !rewrite.isBlank()) {
            int slash = target.indexOf('/');
            moved = rewrite.replaceAll("^/+|/+$", "") + (slash < 0 ? "/" + target : target.substring(slash));
        }
        if (!REPOSITORY.matcher(moved).matches()) {
            throw new IOException(target + " becomes " + moved + ", not a valid repository name");
        }
        return moved;
    }
}
