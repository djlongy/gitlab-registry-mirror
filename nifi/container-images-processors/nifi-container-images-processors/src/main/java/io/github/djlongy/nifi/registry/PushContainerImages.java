package io.github.djlongy.nifi.registry;

import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream;
import org.apache.nifi.annotation.behavior.InputRequirement;
import org.apache.nifi.annotation.behavior.WritesAttribute;
import org.apache.nifi.annotation.behavior.WritesAttributes;
import org.apache.nifi.annotation.documentation.CapabilityDescription;
import org.apache.nifi.annotation.documentation.Tags;
import org.apache.nifi.components.PropertyDescriptor;
import org.apache.nifi.components.ValidationContext;
import org.apache.nifi.components.ValidationResult;
import org.apache.nifi.expression.ExpressionLanguageScope;
import org.apache.nifi.flowfile.FlowFile;
import org.apache.nifi.processor.AbstractProcessor;
import org.apache.nifi.processor.ProcessContext;
import org.apache.nifi.processor.ProcessSession;
import org.apache.nifi.processor.Relationship;
import org.apache.nifi.processor.exception.ProcessException;
import org.apache.nifi.processor.util.StandardValidators;
import org.apache.nifi.ssl.SSLContextProvider;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;

@Tags({"container", "image", "oci", "docker", "registry", "push", "mirror", "air-gap", "skopeo"})
@CapabilityDescription("Pushes the container images in a mirror.py export tar (the FlowFile content, for example after "
        + "UnpackContent on a .tar.ffv3) into an OCI or Docker registry over the registry HTTP API, digests and tags "
        + "unchanged, with no skopeo or docker on the NiFi host. Every image is checked against its recorded digest and "
        + "the target tags are checked before anything is pushed, so a refusal pushes nothing. A tag already at its "
        + "digest is skipped. Blobs the registry holds are not sent again. Works with registry:2 (open, Basic or token "
        + "auth), Quay, Artifactory, Harbor, GitLab and other registries that follow the distribution API.")
@InputRequirement(InputRequirement.Requirement.INPUT_REQUIRED)
@WritesAttributes({
        @WritesAttribute(attribute = "registry.host", description = "The registry the images went to"),
        @WritesAttribute(attribute = "registry.images.pushed", description = "How many image tags were pushed"),
        @WritesAttribute(attribute = "registry.images.present", description = "How many were already at their digest"),
        @WritesAttribute(attribute = "registry.images", description = "One line per image tag: repository:tag@digest"),
        @WritesAttribute(attribute = "registry.error", description = "Why the push failed, on the failure relationship")
})
public class PushContainerImages extends AbstractProcessor {

    static final PropertyDescriptor REGISTRY_URL = new PropertyDescriptor.Builder()
            .name("Registry URL")
            .description("The registry API base, scheme included: https://registry.example.internal or "
                    + "http://registry.example.internal:5000 for a plain-HTTP registry. Images go to <URL>/v2/<repository>.")
            .required(true)
            .expressionLanguageSupported(ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
            .addValidator(StandardValidators.URL_VALIDATOR)
            .build();

    static final PropertyDescriptor USERNAME = new PropertyDescriptor.Builder()
            .name("Username")
            .description("Account for the registry (a Quay robot account, an Artifactory user or identity token "
                    + "owner, an htpasswd user). Leave empty for an open registry.")
            .required(false)
            .expressionLanguageSupported(ExpressionLanguageScope.ENVIRONMENT)
            .addValidator(StandardValidators.NON_BLANK_VALIDATOR)
            .build();

    static final PropertyDescriptor PASSWORD = new PropertyDescriptor.Builder()
            .name("Password")
            .description("Password or token for Username.")
            .required(false)
            .sensitive(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor SSL_CONTEXT = new PropertyDescriptor.Builder()
            .name("SSL Context Service")
            .description("Trust store (and keystore, for client certificates) for an https:// registry. Empty: the "
                    + "Java default trust store.")
            .required(false)
            .identifiesControllerService(SSLContextProvider.class)
            .build();

    static final PropertyDescriptor REWRITE_BASE_PATH = new PropertyDescriptor.Builder()
            .name("Rewrite Base Path")
            .description("Replace the first path segment of every repository: team/prom/prometheus with new-org "
                    + "becomes new-org/prom/prometheus. For Artifactory, the Docker repository key. Empty: unchanged.")
            .required(false)
            .expressionLanguageSupported(ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
            .addValidator(StandardValidators.NON_BLANK_VALIDATOR)
            .build();

    static final PropertyDescriptor STRIP_BASE_PATH = new PropertyDescriptor.Builder()
            .name("Strip Base Path")
            .description("Remove the first path segment of every repository: team/prom/prometheus becomes "
                    + "prom/prometheus. Not together with Rewrite Base Path.")
            .required(true)
            .allowableValues("true", "false")
            .defaultValue("false")
            .build();

    static final PropertyDescriptor MOVE_TAGS = new PropertyDescriptor.Builder()
            .name("Move Existing Tags")
            .description("A tag that already points at another digest is refused (and nothing is pushed) unless "
                    + "this is true.")
            .required(true)
            .allowableValues("true", "false")
            .defaultValue("false")
            .build();

    static final PropertyDescriptor CONNECT_TIMEOUT = new PropertyDescriptor.Builder()
            .name("Connect Timeout")
            .required(true)
            .defaultValue("30 secs")
            .addValidator(StandardValidators.TIME_PERIOD_VALIDATOR)
            .build();

    static final PropertyDescriptor REQUEST_TIMEOUT = new PropertyDescriptor.Builder()
            .name("Request Timeout")
            .description("Longest single request, a layer upload included: allow for the largest layer.")
            .required(true)
            .defaultValue("60 mins")
            .addValidator(StandardValidators.TIME_PERIOD_VALIDATOR)
            .build();

    static final Relationship REL_SUCCESS = new Relationship.Builder()
            .name("success").description("Every image in the tar is in the registry at its digest").build();
    static final Relationship REL_FAILURE = new Relationship.Builder()
            .name("failure").description("The content is not a valid export, a tag would move, or the registry "
                    + "refused a request; registry.error says which").build();

    private static final List<PropertyDescriptor> PROPERTIES = List.of(REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT,
            REWRITE_BASE_PATH, STRIP_BASE_PATH, MOVE_TAGS, CONNECT_TIMEOUT, REQUEST_TIMEOUT);

    @Override
    protected List<PropertyDescriptor> getSupportedPropertyDescriptors() {
        return PROPERTIES;
    }

    @Override
    public Set<Relationship> getRelationships() {
        return Set.of(REL_SUCCESS, REL_FAILURE);
    }

    @Override
    protected Collection<ValidationResult> customValidate(ValidationContext context) {
        List<ValidationResult> results = new ArrayList<>();
        if (context.getProperty(STRIP_BASE_PATH).asBoolean() && context.getProperty(REWRITE_BASE_PATH).isSet()) {
            results.add(new ValidationResult.Builder().subject(STRIP_BASE_PATH.getDisplayName()).valid(false)
                    .explanation("set Strip Base Path or Rewrite Base Path, not both").build());
        }
        return results;
    }

    @Override
    public void onTrigger(ProcessContext context, ProcessSession session) throws ProcessException {
        FlowFile flowFile = session.get();
        if (flowFile == null) {
            return;
        }
        String url = context.getProperty(REGISTRY_URL).evaluateAttributeExpressions(flowFile).getValue();
        SSLContextProvider ssl = context.getProperty(SSL_CONTEXT).asControllerService(SSLContextProvider.class);
        RegistryClient registry;
        try {
            registry = new RegistryClient(url,
                    context.getProperty(USERNAME).evaluateAttributeExpressions().getValue(),
                    context.getProperty(PASSWORD).getValue(),
                    ssl == null ? null : ssl.createContext(),
                    Duration.ofMillis(context.getProperty(CONNECT_TIMEOUT).asTimePeriod(TimeUnit.MILLISECONDS)),
                    Duration.ofMillis(context.getProperty(REQUEST_TIMEOUT).asTimePeriod(TimeUnit.MILLISECONDS)));
        } catch (IllegalArgumentException e) {
            fail(session, flowFile, e.getMessage());
            return;
        }
        String rewrite = context.getProperty(REWRITE_BASE_PATH).evaluateAttributeExpressions(flowFile).getValue();
        boolean strip = context.getProperty(STRIP_BASE_PATH).asBoolean();
        boolean move = context.getProperty(MOVE_TAGS).asBoolean();
        try {
            Map<String, String> attributes = push(session, flowFile, registry, rewrite, strip, move);
            session.transfer(session.putAllAttributes(flowFile, attributes), REL_SUCCESS);
        } catch (IOException | RuntimeException e) {  // anything else would roll back and retry forever
            Throwable cause = e;  // session.read wraps what the callback throws
            while (cause.getCause() != null && !(cause instanceof IOException)) {
                cause = cause.getCause();
            }
            String message = cause.getMessage();
            getLogger().error("Push to {} failed for {}: {}", registry.host(), flowFile, message);
            fail(session, flowFile, message);
        }
    }

    private Map<String, String> push(ProcessSession session, FlowFile flowFile, RegistryClient registry, String rewrite,
                                     boolean strip, boolean move) throws IOException {
        ExportPlan[] holder = new ExportPlan[1];
        session.read(flowFile, in -> holder[0] = ExportPlan.read(in));
        ExportPlan plan = holder[0];

        // Where each image goes, and which are already there. Every check runs before the first push.
        Map<ExportPlan.Image, String> repositories = new LinkedHashMap<>();
        List<ExportPlan.Image> todo = new ArrayList<>();
        List<String> lines = new ArrayList<>();
        int present = 0;
        for (ExportPlan.Image image : plan.images) {
            String repository = ExportPlan.rebase(image.target(), rewrite, strip);
            repositories.put(image, repository);
            String current = registry.manifestDigest(repository, image.tag());
            if (image.digest().equals(current)) {
                present++;
            } else if (current != null && !move) {
                throw new IOException(registry.host() + "/" + repository + ":" + image.tag() + " is " + current
                        + ", the tar has " + image.digest() + "; a tag does not move unless Move Existing Tags is true");
            } else {
                todo.add(image);
            }
            lines.add(repository + ":" + image.tag() + "@" + image.digest());
        }

        // Which repositories still need each blob.
        Map<String, Set<String>> needed = new LinkedHashMap<>();
        for (ExportPlan.Image image : todo) {
            String repository = repositories.get(image);
            for (String blob : image.blobs()) {
                if (!needed.computeIfAbsent(blob, b -> new LinkedHashSet<>()).contains(repository)
                        && !registry.blobExists(repository, blob)) {
                    needed.get(blob).add(repository);
                }
            }
        }
        needed.values().removeIf(Set::isEmpty);
        if (!needed.isEmpty()) {
            session.read(flowFile, in -> uploadBlobs(in, plan, needed, registry));
        }
        if (!needed.isEmpty()) {
            throw new IOException("the tar holds no data for " + needed.keySet());
        }

        // Manifests last: children by digest, then the tag.
        for (ExportPlan.Image image : todo) {
            String repository = repositories.get(image);
            for (ExportPlan.Manifest child : image.children()) {
                registry.putManifest(repository, child.digest(), child.bytes(), child.mediaType());
            }
            registry.putManifest(repository, image.tag(), image.root().bytes(), image.root().mediaType());
            String after = registry.manifestDigest(repository, image.tag());
            if (!image.digest().equals(after)) {
                throw new IOException("digest mismatch after the push: " + repository + ":" + image.tag() + " is " + after);
            }
            getLogger().info("Pushed {}/{}:{}@{}", registry.host(), repository, image.tag(), image.digest());
        }
        return Map.of("registry.host", registry.host(),
                "registry.images.pushed", String.valueOf(todo.size()),
                "registry.images.present", String.valueOf(present),
                "registry.images", String.join("\n", lines));
    }

    /** Stream the tar once, uploading each blob to every repository that needs it. Removes what it sends from needed. */
    private void uploadBlobs(InputStream content, ExportPlan plan, Map<String, Set<String>> needed, RegistryClient registry)
            throws IOException {
        TarArchiveInputStream tar = new TarArchiveInputStream(content);
        TarArchiveEntry entry;
        while ((entry = tar.getNextEntry()) != null && !needed.isEmpty()) {
            String name = ExportPlan.entryName(entry);
            if (!entry.isFile() || entry.isLink()) {
                continue;
            }
            String file = name.substring(name.lastIndexOf('/') + 1);
            String digest = "sha256:" + file;
            Set<String> repositories = needed.get(digest);
            if (repositories == null || !name.equals(plan.blobEntries.get(digest))) {
                continue;
            }
            long size = entry.getSize();
            List<String> targets = new ArrayList<>(repositories);
            if (targets.size() == 1) {
                upload(registry, targets.get(0), digest, tar, size);
            } else {
                // Needed in several repositories: keep a copy, send it once, then mount it (or send it again).
                Path copy = Files.createTempFile("container-images-", ".blob");
                try {
                    try (DigestCheck check = new DigestCheck(tar, digest)) {
                        Files.copy(check, copy, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                    }
                    String first = targets.get(0);
                    registry.uploadBlob(first, digest, () -> open(copy), size);
                    for (String repository : targets.subList(1, targets.size())) {
                        java.net.URI upload = registry.mountOrStartUpload(repository, digest, first);
                        if (upload != null) {  // the registry would not mount it: send it, on the session it opened
                            registry.uploadBlob(repository, digest, () -> open(copy), size, upload);
                        }
                    }
                } finally {
                    Files.deleteIfExists(copy);
                }
            }
            needed.remove(digest);
        }
    }

    private static void upload(RegistryClient registry, String repository, String digest, InputStream tar, long size)
            throws IOException {
        DigestCheck check = new DigestCheck(tar, digest);
        registry.uploadBlob(repository, digest, () -> check, size);
        check.verify();
    }

    private static InputStream open(Path path) {
        try {
            return Files.newInputStream(path);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private static void fail(ProcessSession session, FlowFile flowFile, String message) {
        flowFile = session.putAttribute(flowFile, "registry.error", message == null ? "unknown error" : message);
        session.transfer(session.penalize(flowFile), REL_FAILURE);
    }

    /** Reads a blob from the tar, never closing the tar, and checks its sha256 once it has been read. */
    private static final class DigestCheck extends DigestInputStream {
        private final String expected;
        private String actual;

        DigestCheck(InputStream in, String expected) {
            super(in, Digests.sha256());
            this.expected = expected;
        }

        void verify() throws IOException {
            if (actual == null) {
                MessageDigest md = getMessageDigest();
                actual = "sha256:" + HexFormat.of().formatHex(md.digest());
            }
            if (!actual.equals(expected)) {
                throw new IOException("blob " + expected + " in the tar hashes to " + actual);
            }
        }

        @Override
        public void close() throws IOException {
            verify();  // the tar stays open for the next entry
        }
    }
}
