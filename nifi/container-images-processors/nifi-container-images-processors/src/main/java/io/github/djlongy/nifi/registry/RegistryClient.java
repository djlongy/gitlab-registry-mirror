package io.github.djlongy.nifi.registry;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import javax.net.ssl.SSLContext;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * A client for the OCI distribution (Docker Registry HTTP API v2) push endpoints, with the two
 * authentication schemes registries use: a Bearer token from the realm the 401 names (Quay,
 * Artifactory, Harbor, GitLab, Docker Hub, distribution with token auth), or Basic (distribution
 * with htpasswd). With no credentials it works against an open registry.
 */
class RegistryClient {

    static final String MANIFEST_TYPES = String.join(",",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json");

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Pattern PARAM = Pattern.compile("(\\w+)=\"([^\"]*)\"");
    private static final Pattern NEXT = Pattern.compile("<([^>]+)>\\s*;\\s*rel=\"?next\"?");

    private final URI base;
    private final HttpClient http;
    private final HttpClient noRedirect;
    private final String username;
    private final String password;
    private final Duration timeout;
    private final Map<String, String> authorization = new ConcurrentHashMap<>();

    RegistryClient(String url, String username, String password, SSLContext ssl, Duration connectTimeout, Duration timeout) {
        this.base = URI.create(url.replaceAll("/+$", ""));
        if (!"https".equals(base.getScheme()) && !"http".equals(base.getScheme())) {
            throw new IllegalArgumentException("Registry URL must start with https:// or http://: " + url);
        }
        HttpClient.Builder builder = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .followRedirects(HttpClient.Redirect.NORMAL)
                .connectTimeout(connectTimeout);
        if (ssl != null) {
            builder.sslContext(ssl);
        }
        this.http = builder.build();
        // Blob downloads may redirect to object storage, which must not receive the registry's Authorization.
        this.noRedirect = builder.followRedirects(HttpClient.Redirect.NEVER).build();
        this.username = blankToNull(username);
        this.password = password == null ? "" : password;
        this.timeout = timeout;
    }

    String host() {
        return base.getAuthority();
    }

    /** The digest a tag or digest reference points at, or null when the registry has no such manifest. */
    String manifestDigest(String repository, String reference) throws IOException {
        HttpResponse<byte[]> response = send(repository, "pull", () -> request(v2(repository, "manifests/" + reference))
                .method("HEAD", HttpRequest.BodyPublishers.noBody()).header("Accept", MANIFEST_TYPES));
        if (response.statusCode() == 404) {
            return null;
        }
        expect(response, "HEAD manifest " + repository + ":" + reference, 200);
        String digest = response.headers().firstValue("Docker-Content-Digest").orElse(null);
        if (digest != null) {
            return digest;
        }
        // A registry that leaves the header out: hash the manifest it serves.
        HttpResponse<byte[]> body = send(repository, "pull", () -> request(v2(repository, "manifests/" + reference))
                .GET().header("Accept", MANIFEST_TYPES));
        expect(body, "GET manifest " + repository + ":" + reference, 200);
        return Digests.sha256(body.body());
    }

    record Fetched(byte[] bytes, String mediaType) {
    }

    /** A manifest by tag or digest, exactly as the registry stores it. A digest reference is checked against the bytes. */
    Fetched getManifest(String repository, String reference) throws IOException {
        HttpResponse<byte[]> response = send(repository, "pull", () -> request(v2(repository, "manifests/" + reference))
                .GET().header("Accept", MANIFEST_TYPES));
        expect(response, "GET manifest " + repository + ":" + reference, 200);
        if (reference.startsWith("sha256:") && !Digests.sha256(response.body()).equals(reference)) {
            throw new IOException("manifest " + repository + "@" + reference + " does not match its digest");
        }
        String type = response.headers().firstValue("Content-Type").orElse("").split(";")[0].strip();
        return new Fetched(response.body(), type);
    }

    /** A blob's bytes, following a redirect to storage without the registry's credentials. The caller closes it. */
    InputStream openBlob(String repository, String digest) throws IOException {
        HttpRequest get = withAuth(request(v2(repository, "blobs/" + digest)).GET(), repository, "pull").build();
        boolean authorised = false;
        for (int hop = 0; hop < 5; hop++) {
            HttpResponse<InputStream> response;
            try {
                response = noRedirect.send(get, HttpResponse.BodyHandlers.ofInputStream());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException("interrupted: GET blob " + digest, e);
            }
            int code = response.statusCode();
            if (code == 200) {
                return response.body();
            }
            response.body().close();
            if (code == 401 && !authorised && hop == 0) {
                String challenge = response.headers().firstValue("WWW-Authenticate").orElse("");
                authorization.put(scope(repository, "pull"), authorize(challenge, repository, "pull", null));
                get = withAuth(request(v2(repository, "blobs/" + digest)).GET(), repository, "pull").build();
                authorised = true;
                hop--;
                continue;
            }
            if (code / 100 != 3) {
                throw new IOException("GET blob " + digest + " from " + repository + ": HTTP " + code);
            }
            URI next = response.uri().resolve(response.headers().firstValue("Location")
                    .orElseThrow(() -> new IOException("redirect without a Location for blob " + digest)));
            HttpRequest.Builder follow = request(next).GET();
            get = next.getAuthority().equals(base.getAuthority()) ? withAuth(follow, repository, "pull").build() : follow.build();
        }
        throw new IOException("too many redirects for blob " + digest);
    }

    /** Every repository the registry lists to this client (/v2/_catalog), page by page. */
    List<String> catalog() throws IOException {
        return paged(null, "_catalog?n=1000", "repositories", "the catalog");
    }

    /** Every tag of a repository; empty when the repository does not exist. */
    List<String> tags(String repository) throws IOException {
        return paged(repository, repository + "/tags/list?n=1000", "tags", repository);
    }

    private List<String> paged(String repository, String first, String field, String what) throws IOException {
        List<String> out = new java.util.ArrayList<>();
        URI next = URI.create(base + "/v2/" + first);
        Set<URI> seen = new java.util.HashSet<>();
        while (next != null && seen.add(next) && seen.size() <= 1000) {
            URI page = next;
            HttpResponse<byte[]> response = send(repository, "pull", () -> request(page).GET());
            if (response.statusCode() == 404 && repository != null) {
                return out;
            }
            expect(response, "list " + what, 200);
            JsonNode values = JSON.readTree(response.body()).get(field);
            if (values != null) {
                values.forEach(v -> out.add(v.asText()));
            }
            next = response.headers().firstValue("Link").map(NEXT::matcher).filter(Matcher::find)
                    .map(m -> page.resolve(m.group(1))).orElse(null);
        }
        return out;
    }

    boolean blobExists(String repository, String digest) throws IOException {
        HttpResponse<byte[]> response = send(repository, "pull", () -> request(v2(repository, "blobs/" + digest))
                .method("HEAD", HttpRequest.BodyPublishers.noBody()));
        if (response.statusCode() == 404) {
            return false;
        }
        expect(response, "HEAD blob " + digest + " in " + repository, 200);
        return true;
    }

    /** Ask the registry to link a blob it holds in another repository; returns the upload URL when it will not. */
    URI mountOrStartUpload(String repository, String digest, String from) throws IOException {
        String query = from == null ? "" : "?mount=" + encode(digest) + "&from=" + encode(from);
        HttpResponse<byte[]> response = send(repository, "pull,push", from, () -> request(v2(repository, "blobs/uploads/" + query))
                .POST(HttpRequest.BodyPublishers.noBody()));
        if (from != null && response.statusCode() == 201) {
            return null;
        }
        expect(response, "start upload to " + repository, 202);
        return uploadLocation(response, repository);
    }

    /** Upload one blob in a single PUT. The stream is read once: authentication is settled by the POST before it. */
    void uploadBlob(String repository, String digest, Supplier<InputStream> content, long size) throws IOException {
        uploadBlob(repository, digest, content, size, mountOrStartUpload(repository, digest, null));
    }

    /** Upload one blob to an upload URL the registry handed out (a start, or a mount it declined). */
    void uploadBlob(String repository, String digest, Supplier<InputStream> content, long size, URI upload) throws IOException {
        String separator = upload.getQuery() == null ? "?" : "&";
        URI put = URI.create(upload + separator + "digest=" + encode(digest));
        HttpRequest.BodyPublisher body = size == 0 ? HttpRequest.BodyPublishers.noBody()  // a positive length only
                : HttpRequest.BodyPublishers.fromPublisher(HttpRequest.BodyPublishers.ofInputStream(content), size);
        HttpRequest.Builder builder = request(put).header("Content-Type", "application/octet-stream").PUT(body);
        HttpResponse<byte[]> response = exchange(withAuth(builder, repository, "pull,push").build());
        expect(response, "upload blob " + digest + " to " + repository + " (" + put.getRawPath() + ")", 201, 204);
    }

    /** PUT a manifest under a tag or its digest; returns the digest the registry reports. */
    String putManifest(String repository, String reference, byte[] manifest, String mediaType) throws IOException {
        HttpResponse<byte[]> response = send(repository, "pull,push", () -> request(v2(repository, "manifests/" + reference))
                .header("Content-Type", mediaType).PUT(HttpRequest.BodyPublishers.ofByteArray(manifest)));
        expect(response, "push manifest " + repository + ":" + reference, 201, 200);
        return response.headers().firstValue("Docker-Content-Digest").orElse(Digests.sha256(manifest));
    }

    private HttpResponse<byte[]> send(String repository, String actions, Supplier<HttpRequest.Builder> request) throws IOException {
        return send(repository, actions, null, request);
    }

    /** One request, authorised on a 401 and sent again. from adds pull on another repository (a blob mount). */
    private HttpResponse<byte[]> send(String repository, String actions, String from, Supplier<HttpRequest.Builder> request)
            throws IOException {
        String key = from == null ? actions : actions + "+" + from;
        HttpResponse<byte[]> response = exchange(withAuth(request.get(), repository, key).build());
        if (response.statusCode() != 401) {
            return response;
        }
        String challenge = response.headers().firstValue("WWW-Authenticate").orElse("");
        String header = authorize(challenge, repository, actions, from);
        authorization.put(scope(repository, key), header);
        if (from != null) {  // it covers the target repository too: the PUT after a declined mount uses that key
            authorization.put(scope(repository, actions), header);
        }
        response = exchange(withAuth(request.get(), repository, key).build());
        if (response.statusCode() == 401 || response.statusCode() == 403) {
            throw new IOException(host() + " refused " + (repository == null ? "the catalog" : actions + " on " + repository)
                    + (username == null ? " to an anonymous client; set Username and Password"
                    : " for " + username + "; check the credentials and that the account may " + actions + " there")
                    + " (HTTP " + response.statusCode() + ")");
        }
        return response;
    }

    private String authorize(String challenge, String repository, String actions, String from) throws IOException {
        if (challenge.regionMatches(true, 0, "Basic", 0, 5)) {
            if (username == null) {
                throw new IOException(host() + " asks for a username and password; set Username and Password");
            }
            return basic();
        }
        if (!challenge.regionMatches(true, 0, "Bearer", 0, 6)) {
            throw new IOException(host() + " refused the request with no usable challenge: " + challenge);
        }
        Map<String, String> params = new LinkedHashMap<>();
        Matcher matcher = PARAM.matcher(challenge);
        while (matcher.find()) {
            params.put(matcher.group(1).toLowerCase(), matcher.group(2));
        }
        String realm = params.get("realm");
        if (realm == null) {
            throw new IOException(host() + " sent a Bearer challenge without a realm: " + challenge);
        }
        StringBuilder url = new StringBuilder(realm).append(realm.contains("?") ? "&" : "?");
        if (params.containsKey("service")) {
            url.append("service=").append(encode(params.get("service"))).append('&');
        }
        url.append("scope=").append(encode(repository == null ? "registry:catalog:*" : "repository:" + repository + ":" + actions));
        if (from != null) {
            url.append("&scope=").append(encode("repository:" + from + ":pull"));
        }
        HttpRequest.Builder token = HttpRequest.newBuilder(URI.create(url.toString())).timeout(timeout).GET();
        if (username != null) {
            token.header("Authorization", basic());
        }
        HttpResponse<byte[]> response = exchange(token.build());
        expect(response, "token from " + realm, 200);
        JsonNode body = JSON.readTree(response.body());
        JsonNode value = body.hasNonNull("token") ? body.get("token") : body.get("access_token");
        if (value == null || value.asText().isEmpty()) {
            throw new IOException("no token in the answer from " + realm);
        }
        return "Bearer " + value.asText();
    }

    private HttpRequest.Builder withAuth(HttpRequest.Builder builder, String repository, String actions) {
        String header = authorization.get(scope(repository, actions));
        if (header == null && actions.equals("pull")) {
            header = authorization.get(scope(repository, "pull,push"));  // a push token covers reads too
        }
        return header == null ? builder : builder.header("Authorization", header);
    }

    private HttpRequest.Builder request(URI uri) {
        return HttpRequest.newBuilder(uri).timeout(timeout);
    }

    private HttpResponse<byte[]> exchange(HttpRequest request) throws IOException {
        try {
            return http.send(request, HttpResponse.BodyHandlers.ofByteArray());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("interrupted: " + request.method() + " " + request.uri(), e);
        }
    }

    private URI v2(String repository, String rest) {
        return URI.create(base + "/v2/" + repository + "/" + rest);
    }

    /**
     * Where to send the upload. Some registries behind a path (Artifactory's /artifactory/api/docker/<repo-key>, or
     * its repository-path method) answer with a Location that drops that path, which lands the PUT on another
     * repository; such a Location is moved back under this client's base and repository.
     */
    private URI uploadLocation(HttpResponse<byte[]> response, String repository) throws IOException {
        String header = response.headers().firstValue("Location")
                .orElseThrow(() -> new IOException("upload started without a Location header"));
        URI location = response.uri().resolve(header);
        String prefix = base.getRawPath();
        String path = location.getRawPath();
        int at = path.indexOf("/blobs/uploads/");
        // Only the case seen in the field: a base with a path, and a Location that starts at /v2/ without it.
        if (!prefix.isEmpty() && base.getAuthority().equals(location.getAuthority()) && path.startsWith("/v2/") && at >= 0) {
            String id = path.substring(at + "/blobs/uploads/".length());
            return URI.create(base + "/v2/" + repository + "/blobs/uploads/" + id
                    + (location.getRawQuery() == null ? "" : "?" + location.getRawQuery()));
        }
        return location;
    }

    private String basic() {
        return "Basic " + Base64.getEncoder().encodeToString((username + ":" + password).getBytes(StandardCharsets.UTF_8));
    }

    private static String scope(String repository, String actions) {
        return repository + ":" + actions;
    }

    private static void expect(HttpResponse<byte[]> response, String what, Integer... codes) throws IOException {
        if (!List.of(codes).contains(response.statusCode())) {
            String body = new String(response.body(), StandardCharsets.UTF_8);
            throw new IOException(what + ": HTTP " + response.statusCode()
                    + (body.isBlank() ? "" : ": " + body.substring(0, Math.min(300, body.length())).strip()));
        }
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value;
    }
}
