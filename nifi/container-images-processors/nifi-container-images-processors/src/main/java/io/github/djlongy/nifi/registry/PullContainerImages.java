package io.github.djlongy.nifi.registry;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveOutputStream;
import org.apache.commons.compress.archivers.tar.TarConstants;
import org.apache.nifi.annotation.behavior.InputRequirement;
import org.apache.nifi.annotation.behavior.Stateful;
import org.apache.nifi.annotation.behavior.TriggerSerially;
import org.apache.nifi.annotation.behavior.WritesAttribute;
import org.apache.nifi.annotation.behavior.WritesAttributes;
import org.apache.nifi.annotation.documentation.CapabilityDescription;
import org.apache.nifi.annotation.documentation.SeeAlso;
import org.apache.nifi.annotation.documentation.Tags;
import org.apache.nifi.components.PropertyDescriptor;
import org.apache.nifi.components.ValidationContext;
import org.apache.nifi.components.ValidationResult;
import org.apache.nifi.components.state.Scope;
import org.apache.nifi.components.state.StateMap;
import org.apache.nifi.expression.ExpressionLanguageScope;
import org.apache.nifi.flowfile.FlowFile;
import org.apache.nifi.processor.AbstractProcessor;
import org.apache.nifi.processor.DataUnit;
import org.apache.nifi.processor.ProcessContext;
import org.apache.nifi.processor.ProcessSession;
import org.apache.nifi.processor.Relationship;
import org.apache.nifi.processor.exception.ProcessException;
import org.apache.nifi.processor.util.StandardValidators;
import org.apache.nifi.ssl.SSLContextProvider;

import javax.net.ssl.SSLContext;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.DigestInputStream;
import java.security.DigestOutputStream;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;

@Tags({"container", "image", "oci", "docker", "registry", "pull", "export", "mirror", "air-gap", "skopeo"})
@CapabilityDescription("Exports container images from an OCI or Docker registry into a mirror.py export tar (export.json "
        + "plus one skopeo dir: copy per image, digests unchanged, shared layers stored once), with the same attributes "
        + "mirror.py sends to NiFi (Filename, X-Sha256, X-Artifact-Type, X-Artifact-Format). PushContainerImages or "
        + "mirror.py import pushes it on the far side. Scheduled with a Ledger URL, it exports what the mirror pipeline "
        + "synced since the last export, by the digest the ledger records, and remembers what it exported in cluster "
        + "state; it never writes the ledger. Given a FlowFile listing images, one per line, it exports those on demand.")
@SeeAlso(PushContainerImages.class)
@InputRequirement(InputRequirement.Requirement.INPUT_ALLOWED)
@TriggerSerially  // one run at a time: two would read the same state and export the same images
@Stateful(scopes = Scope.CLUSTER, description = "For each repository:tag exported from the ledger, the digest exported. "
        + "Clear the state to export everything in the ledger again.")
@WritesAttributes({
        @WritesAttribute(attribute = "filename", description = "<Filename Prefix><UTC time>.tar, with -001-of-00N when split"),
        @WritesAttribute(attribute = "Filename", description = "The same, as mirror.py sends it"),
        @WritesAttribute(attribute = "Content-Type", description = "application/octet-stream"),
        @WritesAttribute(attribute = "X-Sha256", description = "sha256 of the tar"),
        @WritesAttribute(attribute = "X-Artifact-Type", description = "Artifact Type"),
        @WritesAttribute(attribute = "X-Artifact-Format", description = "tar"),
        @WritesAttribute(attribute = "registry.images", description = "One line per image tag: repository:tag@digest"),
        @WritesAttribute(attribute = "registry.error", description = "Why an on-demand export failed")
})
public class PullContainerImages extends AbstractProcessor {

    static final PropertyDescriptor REGISTRY_URL = new PropertyDescriptor.Builder()
            .fromPropertyDescriptor(PushContainerImages.REGISTRY_URL)
            .description("The registry to export from (the low side's mirror registry): https://registry.example.internal "
                    + "or http://registry.example.internal:5000.")
            .expressionLanguageSupported(ExpressionLanguageScope.ENVIRONMENT)
            .build();

    static final PropertyDescriptor SSL_CONTEXT = new PropertyDescriptor.Builder()
            .fromPropertyDescriptor(PushContainerImages.SSL_CONTEXT)
            .description("Trust store (and keystore) for the registry and the Ledger URL. Empty: the Java default trust store.")
            .build();

    static final String SOURCE_LISTING = "Registry Listing";
    static final String SOURCE_LEDGER = "Ledger";

    static final PropertyDescriptor IMAGE_SOURCE = new PropertyDescriptor.Builder()
            .name("Image Source")
            .description("What a scheduled run exports. Registry Listing: every tag of Repositories in the registry whose "
                    + "digest differs from the last export, with no other system involved. Ledger: what the mirror "
                    + "pipeline's ledger says it synced. A FlowFile listing images is exported on demand either way.")
            .required(true)
            .allowableValues(SOURCE_LISTING, SOURCE_LEDGER)
            .defaultValue(SOURCE_LISTING)
            .build();

    static final PropertyDescriptor REPOSITORIES = new PropertyDescriptor.Builder()
            .name("Repositories")
            .description("For Registry Listing: repositories to export, separated by commas or new lines. A name ending "
                    + "in * matches every repository in the registry's catalog with that prefix (team/*), and * alone "
                    + "matches all; a plain name needs no catalog access.")
            .required(false)
            .expressionLanguageSupported(ExpressionLanguageScope.ENVIRONMENT)
            .addValidator(StandardValidators.NON_BLANK_VALIDATOR)
            .build();

    static final PropertyDescriptor TAG_FILTER = new PropertyDescriptor.Builder()
            .name("Tag Filter")
            .description("For Registry Listing: a regular expression a tag must match in full. The default skips "
                    + "sha256-... tags, which hold signatures and attestations rather than images.")
            .required(true)
            .defaultValue("(?!sha256-).+")
            .addValidator(StandardValidators.REGULAR_EXPRESSION_VALIDATOR)
            .build();

    static final PropertyDescriptor INITIAL_RUN = new PropertyDescriptor.Builder()
            .name("Initial Run")
            .description("What the first scheduled run does when the state is empty (a new flow, cleared state, or a "
                    + "NiFi rebuilt from scratch). Export All: export everything it finds; the far side skips what it "
                    + "already holds. Record Only: remember the current digests and export only later changes.")
            .required(true)
            .allowableValues("Export All", "Record Only")
            .defaultValue("Export All")
            .build();

    static final PropertyDescriptor LEDGER_URL = new PropertyDescriptor.Builder()
            .name("Ledger URL")
            .description("For Ledger: the mirror pipeline's ledger, read only. For GitLab, https://<gitlab>/api/v4/"
                    + "projects/<id>/packages/generic/registry-mirror-ledger/v2/ledger.json.")
            .required(false)
            .expressionLanguageSupported(ExpressionLanguageScope.ENVIRONMENT)
            .addValidator(StandardValidators.URL_VALIDATOR)
            .build();

    static final PropertyDescriptor LEDGER_TOKEN = new PropertyDescriptor.Builder()
            .name("Ledger Token")
            .description("Token for the Ledger URL, for GitLab an access token with read_api.")
            .required(false)
            .sensitive(true)
            .addValidator(StandardValidators.NON_EMPTY_VALIDATOR)
            .build();

    static final PropertyDescriptor LEDGER_TOKEN_HEADER = new PropertyDescriptor.Builder()
            .name("Ledger Token Header")
            .description("The header that carries the Ledger Token: PRIVATE-TOKEN for GitLab, Authorization for a "
                    + "server that takes 'Bearer <token>' (put 'Bearer ' in the token).")
            .required(true)
            .defaultValue("PRIVATE-TOKEN")
            .addValidator(StandardValidators.NON_BLANK_VALIDATOR)
            .build();

    static final PropertyDescriptor FILENAME_PREFIX = new PropertyDescriptor.Builder()
            .name("Filename Prefix")
            .description("Start of each tar's name, so a NiFi after the diode can route on the name before unpacking.")
            .required(true)
            .defaultValue("container-images-")
            .addValidator(StandardValidators.NON_BLANK_VALIDATOR)
            .build();

    static final PropertyDescriptor ARTIFACT_TYPE = new PropertyDescriptor.Builder()
            .name("Artifact Type")
            .description("The X-Artifact-Type attribute.")
            .required(true)
            .defaultValue("container-images")
            .addValidator(StandardValidators.NON_BLANK_VALIDATOR)
            .build();

    static final PropertyDescriptor MAX_SIZE = new PropertyDescriptor.Builder()
            .name("Max Export Size")
            .description("Split the export into tars of at most this size, each complete on its own; an image larger "
                    + "than this goes in a tar of its own. Empty: one tar.")
            .required(false)
            .addValidator(StandardValidators.DATA_SIZE_VALIDATOR)
            .build();

    static final Relationship REL_SUCCESS = new Relationship.Builder()
            .name("success").description("Export tars").build();
    static final Relationship REL_ORIGINAL = new Relationship.Builder()
            .name("original").description("An on-demand request once its images are exported").build();
    static final Relationship REL_FAILURE = new Relationship.Builder()
            .name("failure").description("An on-demand request that could not be exported; registry.error says why").build();

    private static final List<PropertyDescriptor> PROPERTIES = List.of(REGISTRY_URL, PushContainerImages.USERNAME,
            PushContainerImages.PASSWORD, SSL_CONTEXT, IMAGE_SOURCE, REPOSITORIES, TAG_FILTER, INITIAL_RUN,
            LEDGER_URL, LEDGER_TOKEN, LEDGER_TOKEN_HEADER,
            PushContainerImages.REWRITE_BASE_PATH, PushContainerImages.STRIP_BASE_PATH, FILENAME_PREFIX, ARTIFACT_TYPE,
            MAX_SIZE, PushContainerImages.CONNECT_TIMEOUT, PushContainerImages.REQUEST_TIMEOUT);
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'");

    @Override
    protected List<PropertyDescriptor> getSupportedPropertyDescriptors() {
        return PROPERTIES;
    }

    @Override
    public Set<Relationship> getRelationships() {
        return Set.of(REL_SUCCESS, REL_ORIGINAL, REL_FAILURE);
    }

    @Override
    protected Collection<ValidationResult> customValidate(ValidationContext context) {
        List<ValidationResult> results = new ArrayList<>();
        if (context.getProperty(PushContainerImages.STRIP_BASE_PATH).asBoolean()
                && context.getProperty(PushContainerImages.REWRITE_BASE_PATH).isSet()) {
            results.add(new ValidationResult.Builder().subject("Strip Base Path").valid(false)
                    .explanation("set Strip Base Path or Rewrite Base Path, not both").build());
        }
        boolean ledger = SOURCE_LEDGER.equals(context.getProperty(IMAGE_SOURCE).getValue());
        if (ledger && !context.getProperty(LEDGER_URL).isSet()) {
            results.add(new ValidationResult.Builder().subject("Ledger URL").valid(false)
                    .explanation("Image Source Ledger needs a Ledger URL").build());
        }
        return results;
    }

    /** One image tag to export: where it is, the digest to take, and the repository it is recorded under. */
    record Wanted(String repository, String tag, String digest, String target) {
    }

    @Override
    public void onTrigger(ProcessContext context, ProcessSession session) throws ProcessException {
        FlowFile request = session.get();
        String ledgerUrl = context.getProperty(LEDGER_URL).evaluateAttributeExpressions().getValue();
        boolean ledgerSource = SOURCE_LEDGER.equals(context.getProperty(IMAGE_SOURCE).getValue());
        boolean scheduled = ledgerSource ? ledgerUrl != null : context.getProperty(REPOSITORIES).isSet();
        if (request == null && (!scheduled || !isPrimaryOrStandalone())) {
            context.yield();  // nothing asked for; on a cluster, only the primary node reads the ledger
            return;
        }
        SSLContextProvider sslProvider = context.getProperty(SSL_CONTEXT).asControllerService(SSLContextProvider.class);
        SSLContext ssl = sslProvider == null ? null : sslProvider.createContext();
        Duration connect = Duration.ofMillis(context.getProperty(PushContainerImages.CONNECT_TIMEOUT).asTimePeriod(TimeUnit.MILLISECONDS));
        Duration timeout = Duration.ofMillis(context.getProperty(PushContainerImages.REQUEST_TIMEOUT).asTimePeriod(TimeUnit.MILLISECONDS));
        String host = "the registry";
        try {
            RegistryClient registry = new RegistryClient(
                    context.getProperty(REGISTRY_URL).evaluateAttributeExpressions().getValue(),
                    context.getProperty(PushContainerImages.USERNAME).evaluateAttributeExpressions().getValue(),
                    context.getProperty(PushContainerImages.PASSWORD).getValue(), ssl, connect, timeout);
            host = registry.host();
            if (request != null) {
                List<Wanted> wanted = requested(session, request, registry, context);
                List<FlowFile> out = export(context, session, request, registry, wanted);
                session.transfer(out, REL_SUCCESS);
                session.transfer(request, REL_ORIGINAL);
                return;
            }
            Map<String, String> exported = new HashMap<>(session.getState(Scope.CLUSTER).toMap());
            Set<String> seen = new java.util.HashSet<>();
            List<Wanted> wanted = ledgerSource ? fromLedger(context, registry, ledgerUrl, ssl, connect, timeout, exported, seen)
                    : fromListing(context, registry, exported, seen);
            // Remember only what the source still lists, so the state does not grow with every tag ever seen.
            Map<String, String> next = new HashMap<>();
            exported.forEach((key, digest) -> {
                if (seen.contains(key)) {
                    next.put(key, digest);
                }
            });
            next.put(INITIALISED, "true");
            if (exported.isEmpty() && "Record Only".equals(context.getProperty(INITIAL_RUN).getValue())) {
                wanted.forEach(w -> next.put(w.repository() + ":" + w.tag(), w.digest()));
                session.setState(next, Scope.CLUSTER);
                getLogger().info("Initial run: recorded {} image tag(s) without exporting them", wanted.size());
                return;
            }
            List<FlowFile> out = wanted.isEmpty() ? List.of() : export(context, session, null, registry, wanted);
            session.transfer(out, REL_SUCCESS);
            wanted.forEach(w -> next.put(w.repository() + ":" + w.tag(), w.digest()));  // what export kept
            // Cached by the session and written on commit, with the tars: both or neither.
            session.setState(next, Scope.CLUSTER);
            if (out.isEmpty()) {
                context.yield();
            }
        } catch (IOException | RuntimeException e) {
            Throwable cause = e;
            while (cause.getCause() != null && !(cause instanceof IOException)) {
                cause = cause.getCause();
            }
            String message = String.valueOf(cause.getMessage());
            getLogger().error("Export from {} failed: {}", host, message);
            if (request != null) {
                session.transfer(session.penalize(session.putAttribute(request, "registry.error", message)), REL_FAILURE);
            } else {
                session.rollback();  // no tars without their state, no state without its tars
                context.yield();
            }
        }
    }

    private static final String INITIALISED = "__initialised";

    /** Registry Listing: every matching tag whose digest differs from the last export. */
    private List<Wanted> fromListing(ProcessContext context, RegistryClient registry, Map<String, String> exported,
                                     Set<String> seen) throws IOException {
        java.util.regex.Pattern tagFilter = java.util.regex.Pattern.compile(context.getProperty(TAG_FILTER).getValue());
        List<String> catalog = null;
        Set<String> repositories = new java.util.LinkedHashSet<>();
        for (String entry : context.getProperty(REPOSITORIES).evaluateAttributeExpressions().getValue().split("[,\\s]+")) {
            if (entry.isBlank()) {
                continue;
            }
            if (entry.endsWith("*")) {
                if (catalog == null) {
                    catalog = registry.catalog();
                }
                String prefix = entry.substring(0, entry.length() - 1);
                catalog.stream().filter(r -> r.startsWith(prefix)).forEach(repositories::add);
            } else {
                repositories.add(entry);
            }
        }
        List<Wanted> wanted = new ArrayList<>();
        for (String repository : repositories) {
            for (String tag : registry.tags(repository)) {
                if (!tagFilter.matcher(tag).matches()) {
                    continue;
                }
                if (!ExportPlan.TAG.matcher(tag).matches() || !ExportPlan.REPOSITORY.matcher(repository).matches()) {
                    getLogger().warn("Skipping {}:{}: not a valid repository and tag", repository, tag);
                    continue;
                }
                seen.add(repository + ":" + tag);
                String digest = registry.manifestDigest(repository, tag);  // resolved once; pulled by this digest
                if (digest != null && !digest.equals(exported.get(repository + ":" + tag))) {
                    wanted.add(new Wanted(repository, tag, digest, target(context, repository)));
                }
            }
        }
        return wanted;
    }

    private boolean isPrimaryOrStandalone() {
        return !getNodeTypeProvider().isClustered() || getNodeTypeProvider().isPrimary();
    }

    /** On demand: one reference per line, repository:tag or repository@sha256:..., in the source registry. */
    private List<Wanted> requested(ProcessSession session, FlowFile request, RegistryClient registry, ProcessContext context)
            throws IOException {
        String[] text = new String[1];
        session.read(request, in -> text[0] = new String(in.readAllBytes(), StandardCharsets.UTF_8));
        List<Wanted> wanted = new ArrayList<>();
        for (String line : text[0].split("\\R")) {
            String reference = line.replaceAll("#.*", "").strip();
            if (reference.isEmpty()) {
                continue;
            }
            // [host/]repository[:tag][@sha256:...]; the host, when given, must be this registry
            String host = registry.host();
            String bareHost = host.replaceAll(":443$", "");
            for (String prefix : List.of(host + "/", bareHost + "/")) {
                if (reference.startsWith(prefix)) {
                    reference = reference.substring(prefix.length());
                    break;
                }
            }
            String digest = null;
            int at = reference.indexOf('@');
            if (at >= 0) {
                digest = reference.substring(at + 1);
                reference = reference.substring(0, at);
            }
            int colon = reference.lastIndexOf(':');
            String tag = colon > reference.lastIndexOf('/') ? reference.substring(colon + 1) : null;
            String repository = tag == null ? reference : reference.substring(0, colon);
            if (!ExportPlan.REPOSITORY.matcher(repository).matches() || (tag != null && !ExportPlan.TAG.matcher(tag).matches())
                    || (digest != null && !ExportPlan.DIGEST.matcher(digest).matches())) {
                throw new IOException("not an image reference in " + host + ": " + line.strip());
            }
            if (digest == null) {
                tag = tag == null ? "latest" : tag;
                digest = registry.manifestDigest(repository, tag);
                if (digest == null) {
                    throw new IOException(host + "/" + repository + ":" + tag + " is not in the registry");
                }
            } else if (tag == null) {
                tag = "sha256-" + digest.substring("sha256:".length(), "sha256:".length() + 12);
            }
            wanted.add(new Wanted(repository, tag, digest, target(context, repository)));
        }
        if (wanted.isEmpty()) {
            throw new IOException("the request lists no images");
        }
        return wanted;
    }

    /** Ledger mode: every synced tag whose digest differs from what this processor last exported. */
    private List<Wanted> fromLedger(ProcessContext context, RegistryClient registry, String url, SSLContext ssl,
                                    Duration connect, Duration timeout, Map<String, String> exported, Set<String> seen)
            throws IOException {
        HttpClient.Builder builder = HttpClient.newBuilder().connectTimeout(connect).followRedirects(HttpClient.Redirect.NORMAL);
        if (ssl != null) {
            builder.sslContext(ssl);
        }
        HttpRequest.Builder get = HttpRequest.newBuilder(URI.create(url)).timeout(timeout).GET();
        String token = context.getProperty(LEDGER_TOKEN).getValue();
        if (token != null) {
            get.header(context.getProperty(LEDGER_TOKEN_HEADER).getValue(), token);
        }
        HttpResponse<byte[]> response;
        try {
            response = builder.build().send(get.build(), HttpResponse.BodyHandlers.ofByteArray());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("interrupted reading the ledger", e);
        }
        if (response.statusCode() != 200) {
            throw new IOException("ledger " + url + ": HTTP " + response.statusCode());
        }
        JsonNode ledger = JSON.readTree(response.body());
        String owner = ledger.path("registry").asText("");
        if (!owner.isEmpty() && !owner.equals(registry.host())) {
            throw new IOException("the ledger belongs to " + owner + ", not " + registry.host() + "; set Registry URL to it");
        }
        List<Wanted> wanted = new ArrayList<>();
        ledger.path("synced").fields().forEachRemaining(entry -> {
            String key = entry.getKey();
            int colon = key.lastIndexOf(':');
            JsonNode record = entry.getValue();
            String digest = record.path("digest").asText("");
            if (colon < 0 || !ExportPlan.DIGEST.matcher(digest).matches()) {
                getLogger().warn("Skipping ledger entry {}: no valid digest", key);
                return;
            }
            String repository = key.substring(0, colon);
            List<String> tags = new ArrayList<>();
            record.path("tags").forEach(t -> tags.add(t.asText()));
            if (tags.isEmpty()) {
                tags.add(key.substring(colon + 1));
            }
            for (String tag : tags) {
                if (!ExportPlan.TAG.matcher(tag).matches() || !ExportPlan.REPOSITORY.matcher(repository).matches()) {
                    getLogger().warn("Skipping ledger entry {}:{}: not a valid repository and tag", repository, tag);
                    continue;
                }
                seen.add(repository + ":" + tag);
                if (!digest.equals(exported.get(repository + ":" + tag))) {
                    wanted.add(new Wanted(repository, tag, digest, repository));
                }
            }
        });
        for (int i = 0; i < wanted.size(); i++) {
            Wanted w = wanted.get(i);
            wanted.set(i, new Wanted(w.repository(), w.tag(), w.digest(), target(context, w.repository())));
        }
        return wanted;
    }

    private String target(ProcessContext context, String repository) throws IOException {
        return ExportPlan.rebase(repository,
                context.getProperty(PushContainerImages.REWRITE_BASE_PATH).evaluateAttributeExpressions().getValue(),
                context.getProperty(PushContainerImages.STRIP_BASE_PATH).asBoolean());
    }

    /** An image ready to write: its manifests (root first) and the blobs they reference, with their sizes. */
    record Image(Wanted wanted, byte[] root, Map<String, byte[]> children, Map<String, Long> blobs) {
        long size() {
            return blobs.values().stream().mapToLong(Long::longValue).sum();
        }
    }

    private List<FlowFile> export(ProcessContext context, ProcessSession session, FlowFile parent, RegistryClient registry,
                                  List<Wanted> wanted) throws IOException {
        List<Image> images = new ArrayList<>();
        Map<String, Image> byDigest = new HashMap<>();
        for (Wanted w : wanted) {
            Image known = byDigest.get(w.repository() + "@" + w.digest());
            if (known != null) {
                images.add(new Image(w, known.root(), known.children(), known.blobs()));
                continue;
            }
            Image image;
            try {
                image = plan(registry, w);
            } catch (IOException e) {
                if (parent != null) {
                    throw e;
                }
                // A digest, child manifest or blob the registry no longer has (garbage-collected, or sync is
                // mid-run): this image waits for the next run, the others go now.
                getLogger().warn("Skipping {}:{}@{} this run: {}", w.repository(), w.tag(), w.digest(), e.getMessage());
                continue;
            }
            byDigest.put(w.repository() + "@" + w.digest(), image);
            images.add(image);
        }
        // Wanted entries skipped above are not exported, so they are not recorded either.
        wanted.retainAll(images.stream().map(Image::wanted).toList());

        List<List<Image>> batches = batches(images, context.getProperty(MAX_SIZE).isSet()
                ? context.getProperty(MAX_SIZE).asDataSize(DataUnit.B).longValue() : 0);
        String stamp = ZonedDateTime.now(ZoneOffset.UTC).format(STAMP);
        String prefix = context.getProperty(FILENAME_PREFIX).getValue();
        List<FlowFile> created = new ArrayList<>();
        try {
            for (int n = 0; n < batches.size(); n++) {
                String name = prefix + stamp + (batches.size() == 1 ? "" : String.format("-%03d-of-%03d", n + 1, batches.size())) + ".tar";
                List<Image> batch = batches.get(n);
                FlowFile flowFile = parent == null ? session.create() : session.create(parent);
                created.add(flowFile);
                MessageDigest sha = Digests.sha256();
                flowFile = session.write(flowFile, out -> writeTar(new DigestOutputStream(out, sha), batch, registry));
                created.set(created.size() - 1, flowFile);
                Map<String, String> attributes = new LinkedHashMap<>();
                attributes.put("filename", name);
                attributes.put("Filename", name);
                attributes.put("Content-Type", "application/octet-stream");
                attributes.put("mime.type", "application/x-tar");
                attributes.put("X-Sha256", HexFormat.of().formatHex(sha.digest()));
                attributes.put("X-Artifact-Type", context.getProperty(ARTIFACT_TYPE).getValue());
                attributes.put("X-Artifact-Format", "tar");
                attributes.put("registry.host", registry.host());
                attributes.put("registry.images", String.join("\n", batch.stream()
                        .map(i -> i.wanted().target() + ":" + i.wanted().tag() + "@" + i.wanted().digest()).toList()));
                flowFile = session.putAllAttributes(flowFile, attributes);
                created.set(created.size() - 1, flowFile);
                getLogger().info("Exported {} image tag(s) from {} as {}", batch.size(), registry.host(), name);
            }
        } catch (RuntimeException e) {  // session.write wraps what the callback throws
            session.remove(created);
            Throwable cause = e;
            while (cause.getCause() != null && !(cause instanceof IOException)) {
                cause = cause.getCause();
            }
            throw new IOException(cause.getMessage(), cause);
        }
        return created;
    }

    /** The manifests and blob sizes of one image, every blob confirmed present before anything is written. */
    private static Image plan(RegistryClient registry, Wanted w) throws IOException {
        RegistryClient.Fetched root = registry.getManifest(w.repository(), w.digest());
        Map<String, byte[]> children = new LinkedHashMap<>();
        Map<String, Long> blobs = new LinkedHashMap<>();
        JsonNode json = JSON.readTree(root.bytes());
        if (json.has("manifests")) {
            for (JsonNode child : json.get("manifests")) {
                String digest = child.path("digest").asText();
                if (!ExportPlan.DIGEST.matcher(digest).matches()) {
                    throw new IOException("unsupported child digest " + digest);
                }
                byte[] bytes = registry.getManifest(w.repository(), digest).bytes();
                children.put(digest, bytes);
                blobsOf(JSON.readTree(bytes), blobs);
            }
        } else {
            blobsOf(json, blobs);
        }
        for (String blob : blobs.keySet()) {
            if (!registry.blobExists(w.repository(), blob)) {
                throw new IOException("blob " + blob + " is missing");
            }
        }
        return new Image(w, root.bytes(), children, blobs);
    }

    private static void blobsOf(JsonNode manifest, Map<String, Long> blobs) throws IOException {
        List<JsonNode> descriptors = new ArrayList<>();
        if (manifest.has("config")) {
            descriptors.add(manifest.get("config"));
        }
        manifest.path("layers").forEach(descriptors::add);
        for (JsonNode d : descriptors) {
            String digest = d.path("digest").asText();
            if (!ExportPlan.DIGEST.matcher(digest).matches() || !d.has("size")) {
                throw new IOException("unsupported descriptor " + d);
            }
            if (!d.has("urls")) {  // a foreign layer stays where it is, as skopeo does
                blobs.put(digest, d.get("size").asLong());
            }
        }
    }

    /** Greedy split by size, in order; an image over the cap goes in a batch of its own. */
    static List<List<Image>> batches(List<Image> images, long cap) {
        List<List<Image>> batches = new ArrayList<>();
        List<Image> current = new ArrayList<>();
        long used = 0;
        for (Image image : images) {
            long size = image.size();
            if (cap > 0 && !current.isEmpty() && used + size > cap) {
                batches.add(current);
                current = new ArrayList<>();
                used = 0;
            }
            current.add(image);
            used += size;
        }
        if (!current.isEmpty()) {
            batches.add(current);
        }
        return batches;
    }

    /** The mirror.py export layout: NNNNNN/ skopeo dir: copies, shared blobs as hard links, export.json last. */
    private void writeTar(OutputStream out, List<Image> batch, RegistryClient registry) throws IOException {
        TarArchiveOutputStream tar = new TarArchiveOutputStream(out);
        tar.setLongFileMode(TarArchiveOutputStream.LONGFILE_POSIX);
        tar.setBigNumberMode(TarArchiveOutputStream.BIGNUMBER_POSIX);
        Map<String, String> written = new HashMap<>();  // blob or manifest digest -> first path in the tar
        ArrayNode index = JSON.createArrayNode();
        for (int n = 0; n < batch.size(); n++) {
            Image image = batch.get(n);
            String dir = String.format("%06d", n);
            directory(tar, "./" + dir + "/");
            file(tar, "./" + dir + "/version", "Directory Transport Version: 1.1\n".getBytes(StandardCharsets.UTF_8));
            file(tar, "./" + dir + "/manifest.json", image.root());
            for (Map.Entry<String, byte[]> child : image.children().entrySet()) {
                file(tar, "./" + dir + "/" + hex(child.getKey()) + ".manifest.json", child.getValue());
            }
            for (Map.Entry<String, Long> blob : image.blobs().entrySet()) {
                String path = "./" + dir + "/" + hex(blob.getKey());
                String first = written.putIfAbsent(blob.getKey(), path);
                if (first != null) {
                    TarArchiveEntry link = new TarArchiveEntry(path, TarConstants.LF_LINK);
                    link.setLinkName(first);
                    tar.putArchiveEntry(link);
                    tar.closeArchiveEntry();
                    continue;
                }
                TarArchiveEntry entry = new TarArchiveEntry(path);
                entry.setSize(blob.getValue());
                tar.putArchiveEntry(entry);
                MessageDigest sha = Digests.sha256();
                long copied;
                try (InputStream in = new DigestInputStream(registry.openBlob(image.wanted().repository(), blob.getKey()), sha)) {
                    copied = in.transferTo(tar);
                }
                if (copied != blob.getValue() || !("sha256:" + HexFormat.of().formatHex(sha.digest())).equals(blob.getKey())) {
                    throw new IOException("blob " + blob.getKey() + " from " + image.wanted().repository()
                            + " did not match its size or digest");
                }
                tar.closeArchiveEntry();
            }
            ObjectNode entry = index.addObject();
            entry.put("dir", dir);
            entry.put("source", registry.host() + "/" + image.wanted().repository() + ":" + image.wanted().tag());
            entry.put("target", image.wanted().target());
            entry.put("tag", image.wanted().tag());
            entry.put("digest", image.wanted().digest());
        }
        ObjectNode root = JSON.createObjectNode();
        root.put("created", ZonedDateTime.now(ZoneOffset.UTC).toString());
        root.set("images", index);
        file(tar, "./" + ExportPlan.INDEX, JSON.writerWithDefaultPrettyPrinter().writeValueAsBytes(root));
        tar.finish();
    }

    private static void directory(TarArchiveOutputStream tar, String name) throws IOException {
        tar.putArchiveEntry(new TarArchiveEntry(name));
        tar.closeArchiveEntry();
    }

    private static void file(TarArchiveOutputStream tar, String name, byte[] data) throws IOException {
        TarArchiveEntry entry = new TarArchiveEntry(name);
        entry.setSize(data.length);
        tar.putArchiveEntry(entry);
        tar.write(data);
        tar.closeArchiveEntry();
    }

    private static String hex(String digest) {
        return digest.substring("sha256:".length());
    }
}
