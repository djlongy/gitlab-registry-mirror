package io.github.djlongy.nifi.registry;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveOutputStream;
import org.apache.commons.compress.archivers.tar.TarConstants;
import org.apache.nifi.util.MockFlowFile;
import org.apache.nifi.util.TestRunner;
import org.apache.nifi.util.TestRunners;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PushContainerImagesTest {

    /** A small distribution registry: open, Basic or Bearer-token auth, blobs and manifests per repository. */
    static final class FakeRegistry {
        final HttpServer server;
        final Map<String, byte[]> blobs = new ConcurrentHashMap<>();       // repo@digest
        final Map<String, byte[]> manifests = new ConcurrentHashMap<>();   // repo:ref
        final AtomicInteger blobUploads = new AtomicInteger();
        final AtomicInteger mounts = new AtomicInteger();
        final String auth;  // "none", "basic", "bearer"

        FakeRegistry(String auth) throws IOException {
            this.auth = auth;
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/token", this::token);
            server.createContext("/ledger.json", exchange -> {
                boolean ok = "read-token".equals(exchange.getRequestHeaders().getFirst("PRIVATE-TOKEN"));
                reply(exchange, ok ? 200 : 401, ok ? ledger : new byte[0]);
            });
            server.createContext("/v2/", this::v2);
            server.start();
        }

        String url() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        private boolean authorised(HttpExchange exchange) throws IOException {
            String header = exchange.getRequestHeaders().getFirst("Authorization");
            boolean ok = switch (auth) {
                case "basic" -> ("Basic " + b64("robot:secret")).equals(header);
                case "bearer" -> "Bearer good-token".equals(header);
                default -> true;
            };
            if (!ok) {
                exchange.getResponseHeaders().add("WWW-Authenticate", auth.equals("basic") ? "Basic realm=\"fake\""
                        : "Bearer realm=\"" + url() + "/token\",service=\"fake\",scope=\"x\"");
                reply(exchange, 401, new byte[0]);
            }
            return ok;
        }

        private void token(HttpExchange exchange) throws IOException {
            boolean ok = ("Basic " + b64("robot:secret")).equals(exchange.getRequestHeaders().getFirst("Authorization"))
                    && exchange.getRequestURI().getQuery().contains("service=fake");
            reply(exchange, ok ? 200 : 401, ok ? "{\"token\":\"good-token\"}".getBytes() : new byte[0]);
        }

        private void v2(HttpExchange exchange) throws IOException {
            exchange.getRequestBody().readAllBytes();
            if (!authorised(exchange)) {
                return;
            }
            String path = exchange.getRequestURI().getPath().substring("/v2/".length());
            String method = exchange.getRequestMethod();
            if (path.equals("_catalog") || path.endsWith("/tags/list")) {
                String repo = path.equals("_catalog") ? null : path.substring(0, path.length() - "/tags/list".length());
                java.util.TreeSet<String> names = new java.util.TreeSet<>();
                for (String key : manifests.keySet()) {
                    int colon = key.indexOf(':');
                    String r = key.substring(0, colon);
                    String tag = key.substring(colon + 1);
                    if (repo == null) {
                        names.add(r);
                    } else if (r.equals(repo) && !tag.startsWith("sha256:")) {
                        names.add(tag);
                    }
                }
                String list = String.join(",", names.stream().map(n -> "\"" + n + "\"").toList());
                reply(exchange, 200, ("{\"" + (repo == null ? "repositories" : "tags") + "\":[" + list + "]}").getBytes());
                return;
            }
            int m = path.lastIndexOf("/manifests/");
            int b = path.lastIndexOf("/blobs/");
            if (m > 0) {
                String key = path.substring(0, m) + ":" + path.substring(m + "/manifests/".length());
                if (method.equals("PUT")) {
                    byte[] body = lastBody;
                    String digest = Digests.sha256(body);
                    manifests.put(key, body);
                    manifests.put(path.substring(0, m) + ":" + digest, body);
                    exchange.getResponseHeaders().add("Docker-Content-Digest", digest);
                    reply(exchange, 201, new byte[0]);
                } else {
                    byte[] body = manifests.get(key);
                    if (body == null) {
                        reply(exchange, 404, new byte[0]);
                        return;
                    }
                    exchange.getResponseHeaders().add("Docker-Content-Digest", Digests.sha256(body));
                    reply(exchange, 200, method.equals("HEAD") ? null : body);
                }
                return;
            }
            String repo = path.substring(0, b);
            String rest = path.substring(b + "/blobs/".length());
            if (rest.startsWith("uploads")) {
                String query = exchange.getRequestURI().getRawQuery();
                if (method.equals("POST") && query != null && query.contains("mount=") && !declineMounts) {
                    Map<String, String> q = query(query);
                    byte[] data = blobs.get(q.get("from") + "@" + q.get("mount"));
                    if (data != null) {
                        blobs.put(repo + "@" + q.get("mount"), data);
                        mounts.incrementAndGet();
                        reply(exchange, 201, new byte[0]);
                        return;
                    }
                }
                if (method.equals("POST")) {
                    exchange.getResponseHeaders().add("Location", "/v2/" + repo + "/blobs/uploads/session-1?state=x");
                    reply(exchange, 202, new byte[0]);
                    return;
                }
                String digest = query(exchange.getRequestURI().getRawQuery()).get("digest");
                if (!Digests.sha256(lastBody).equals(digest)) {
                    reply(exchange, 400, "digest invalid".getBytes());
                    return;
                }
                blobs.put(repo + "@" + digest, lastBody);
                blobUploads.incrementAndGet();
                reply(exchange, 201, new byte[0]);
                return;
            }
            byte[] blob = blobs.get(repo + "@" + rest);
            reply(exchange, blob == null ? 404 : 200, blob == null || method.equals("HEAD") ? null : blob);
        }

        private byte[] lastBody;
        volatile byte[] ledger = "{}".getBytes();
        volatile boolean declineMounts;

        void capture() {
            server.removeContext("/v2/");
            server.createContext("/v2/", exchange -> {
                lastBody = exchange.getRequestBody().readAllBytes();
                v2(exchange);
            });
        }

        private static Map<String, String> query(String raw) {
            Map<String, String> out = new LinkedHashMap<>();
            for (String pair : raw.split("&")) {
                String[] kv = pair.split("=", 2);
                out.put(kv[0], kv.length > 1 ? java.net.URLDecoder.decode(kv[1], StandardCharsets.UTF_8) : "");
            }
            return out;
        }

        private static void reply(HttpExchange exchange, int code, byte[] body) throws IOException {
            exchange.sendResponseHeaders(code, body == null || body.length == 0 ? -1 : body.length);
            if (body != null && body.length > 0) {
                exchange.getResponseBody().write(body);
            }
            exchange.close();
        }
    }

    private FakeRegistry registry;

    @AfterEach
    void stop() {
        if (registry != null) {
            registry.server.stop(0);
        }
    }

    static String b64(String s) {
        return Base64.getEncoder().encodeToString(s.getBytes(StandardCharsets.UTF_8));
    }

    /**
     * An export like mirror.py writes: 000000 a single-platform image, 000001 a two-platform index whose children
     * share a layer with 000000 (stored once, then hard-linked), 000002 the same index under a second tag.
     */
    static byte[] exportTar() throws IOException {
        byte[] layer = "shared layer".getBytes();
        byte[] layerArm = "arm layer".getBytes();
        byte[] config = "{\"architecture\":\"amd64\"}".getBytes();
        byte[] configArm = "{\"architecture\":\"arm64\"}".getBytes();
        byte[] single = manifest(config, layer);
        byte[] amd = manifest(config, layer);
        byte[] arm = manifest(configArm, layerArm);
        byte[] index = ("{\"schemaVersion\":2,\"mediaType\":\"application/vnd.oci.image.index.v1+json\",\"manifests\":["
                + "{\"mediaType\":\"application/vnd.oci.image.manifest.v1+json\",\"digest\":\"" + Digests.sha256(amd)
                + "\",\"size\":" + amd.length + ",\"platform\":{\"architecture\":\"amd64\",\"os\":\"linux\"}},"
                + "{\"mediaType\":\"application/vnd.oci.image.manifest.v1+json\",\"digest\":\"" + Digests.sha256(arm)
                + "\",\"size\":" + arm.length + ",\"platform\":{\"architecture\":\"arm64\",\"os\":\"linux\"}}]}").getBytes();
        String exportJson = "{\"images\":["
                + "{\"dir\":\"000000\",\"target\":\"team/app\",\"tag\":\"1.0\",\"digest\":\"" + Digests.sha256(single) + "\"},"
                + "{\"dir\":\"000001\",\"target\":\"team/multi\",\"tag\":\"2.0\",\"digest\":\"" + Digests.sha256(index) + "\"},"
                + "{\"dir\":\"000002\",\"target\":\"team/multi\",\"tag\":\"latest\",\"digest\":\"" + Digests.sha256(index) + "\"}]}";
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (TarArchiveOutputStream tar = new TarArchiveOutputStream(bytes)) {
            file(tar, "./000000/manifest.json", single);
            file(tar, "./000000/" + hex(config), config);
            file(tar, "./000000/" + hex(layer), layer);
            file(tar, "./000001/manifest.json", index);
            file(tar, "./000001/" + hex(Digests.sha256(amd)) + ".manifest.json", amd);
            file(tar, "./000001/" + hex(Digests.sha256(arm)) + ".manifest.json", arm);
            link(tar, "./000001/" + hex(config), "./000000/" + hex(config));
            link(tar, "./000001/" + hex(layer), "./000000/" + hex(layer));
            file(tar, "./000001/" + hex(configArm), configArm);
            file(tar, "./000001/" + hex(layerArm), layerArm);
            for (String name : List.of("manifest.json", hex(Digests.sha256(amd)) + ".manifest.json",
                    hex(Digests.sha256(arm)) + ".manifest.json", hex(config), hex(layer), hex(configArm), hex(layerArm))) {
                link(tar, "./000002/" + name, "./000001/" + name);
            }
            file(tar, "./export.json", exportJson.getBytes());
        }
        return bytes.toByteArray();
    }

    static byte[] manifest(byte[] config, byte[] layer) {
        return ("{\"schemaVersion\":2,\"mediaType\":\"application/vnd.oci.image.manifest.v1+json\","
                + "\"config\":{\"mediaType\":\"application/vnd.oci.image.config.v1+json\",\"digest\":\""
                + Digests.sha256(config) + "\",\"size\":" + config.length + "},"
                + "\"layers\":[{\"mediaType\":\"application/vnd.oci.image.layer.v1.tar\",\"digest\":\""
                + Digests.sha256(layer) + "\",\"size\":" + layer.length + "}]}").getBytes();
    }

    static String hex(byte[] data) {
        return hex(Digests.sha256(data));
    }

    static String hex(String digest) {
        return digest.substring("sha256:".length());
    }

    static void file(TarArchiveOutputStream tar, String name, byte[] data) throws IOException {
        TarArchiveEntry entry = new TarArchiveEntry(name);
        entry.setSize(data.length);
        tar.putArchiveEntry(entry);
        tar.write(data);
        tar.closeArchiveEntry();
    }

    static void link(TarArchiveOutputStream tar, String name, String target) throws IOException {
        TarArchiveEntry entry = new TarArchiveEntry(name, TarConstants.LF_LINK);
        entry.setLinkName(target);
        tar.putArchiveEntry(entry);
        tar.closeArchiveEntry();
    }

    private TestRunner runner(String auth) throws IOException {
        registry = new FakeRegistry(auth);
        registry.capture();
        TestRunner runner = TestRunners.newTestRunner(PushContainerImages.class);
        runner.setProperty(PushContainerImages.REGISTRY_URL, registry.url());
        if (!auth.equals("none")) {
            runner.setProperty(PushContainerImages.USERNAME, "robot");
            runner.setProperty(PushContainerImages.PASSWORD, "secret");
        }
        return runner;
    }

    private void assertPushed(String prefix) {
        for (String key : List.of(prefix + "app:1.0", prefix + "multi:2.0", prefix + "multi:latest")) {
            assertTrue(registry.manifests.containsKey(key), key + " in " + registry.manifests.keySet());
        }
    }

    @Test
    void pushesEveryImageOnceThroughEachAuthSchemeAndIsIdempotent() throws IOException {
        for (String auth : List.of("none", "basic", "bearer")) {
            TestRunner runner = runner(auth);
            runner.enqueue(exportTar());
            runner.run();
            runner.assertAllFlowFilesTransferred(PushContainerImages.REL_SUCCESS, 1);
            MockFlowFile out = runner.getFlowFilesForRelationship(PushContainerImages.REL_SUCCESS).get(0);
            out.assertAttributeEquals("registry.images.pushed", "3");
            assertPushed("team/");
            // 4 distinct blobs; the shared layer and config go to team/app and team/multi: 2 uploads + 2 mounts.
            assertEquals(4, registry.blobUploads.get(), auth);
            assertEquals(2, registry.mounts.get(), auth);

            runner.clearTransferState();
            runner.enqueue(exportTar());
            runner.run();
            runner.getFlowFilesForRelationship(PushContainerImages.REL_SUCCESS).get(0)
                    .assertAttributeEquals("registry.images.present", "3");
            assertEquals(4, registry.blobUploads.get(), "nothing is sent twice");
            stop();
        }
    }

    @Test
    void rewritesOrStripsTheBasePath() throws IOException {
        TestRunner runner = runner("none");
        runner.setProperty(PushContainerImages.REWRITE_BASE_PATH, "high-dev");
        runner.enqueue(exportTar());
        runner.run();
        assertPushed("high-dev/");

        runner.removeProperty(PushContainerImages.REWRITE_BASE_PATH);
        runner.setProperty(PushContainerImages.STRIP_BASE_PATH, "true");
        runner.enqueue(exportTar());
        runner.run();
        assertPushed("");

        runner.setProperty(PushContainerImages.REWRITE_BASE_PATH, "x");
        runner.assertNotValid();
    }

    @Test
    void refusesToMoveATagAndPushesNothing() throws IOException {
        TestRunner runner = runner("none");
        registry.manifests.put("team/multi:latest", "{\"other\":true}".getBytes());
        runner.enqueue(exportTar());
        runner.run();
        runner.assertAllFlowFilesTransferred(PushContainerImages.REL_FAILURE, 1);
        assertTrue(runner.getFlowFilesForRelationship(PushContainerImages.REL_FAILURE).get(0)
                .getAttribute("registry.error").contains("does not move"));
        assertEquals(0, registry.blobUploads.get());

        runner.clearTransferState();
        runner.setProperty(PushContainerImages.MOVE_TAGS, "true");
        runner.enqueue(exportTar());
        runner.run();
        runner.assertAllFlowFilesTransferred(PushContainerImages.REL_SUCCESS, 1);
    }

    @Test
    void wrongCredentialsAndNonExportsFail() throws IOException {
        TestRunner runner = runner("bearer");
        runner.setProperty(PushContainerImages.PASSWORD, "wrong");
        runner.enqueue(exportTar());
        runner.enqueue("not a tar".getBytes());
        runner.run(2);
        runner.assertAllFlowFilesTransferred(PushContainerImages.REL_FAILURE, 2);
    }

    @Test
    void aDeclinedMountUploadsWithCredentials() throws IOException {
        TestRunner runner = runner("bearer");
        registry.declineMounts = true;  // Harbor, Quay and GitLab answer 202 when they will not mount
        runner.enqueue(exportTar());
        runner.run();
        runner.assertAllFlowFilesTransferred(PushContainerImages.REL_SUCCESS, 1);
        assertEquals(0, registry.mounts.get());
        assertEquals(6, registry.blobUploads.get(), "the shared config and layer go to both repositories");
    }

    @Test
    void pushesAnEmptyLayer() throws IOException {
        byte[] empty = new byte[0];
        byte[] config = "{}".getBytes();
        byte[] image = manifest(config, empty);
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (TarArchiveOutputStream tar = new TarArchiveOutputStream(bytes)) {
            file(tar, "./000000/manifest.json", image);
            file(tar, "./000000/" + hex(config), config);
            file(tar, "./000000/" + hex(empty), empty);
            file(tar, "./export.json", ("{\"images\":[{\"dir\":\"000000\",\"target\":\"team/empty\",\"tag\":\"1\",\"digest\":\""
                    + Digests.sha256(image) + "\"}]}").getBytes());
        }
        TestRunner runner = runner("none");
        runner.enqueue(bytes.toByteArray());
        runner.run();
        runner.assertAllFlowFilesTransferred(PushContainerImages.REL_SUCCESS, 1);
        assertEquals(0, registry.blobs.get("team/empty@" + Digests.sha256(empty)).length);
    }

    @Test
    void rebaseRules() throws IOException {
        assertEquals("new/prom/prometheus", ExportPlan.rebase("team/prom/prometheus", "new", false));
        assertEquals("prom/prometheus", ExportPlan.rebase("team/prom/prometheus", null, true));
        assertEquals("team/app", ExportPlan.rebase("team/app", "", false));
        assertThrows(IOException.class, () -> ExportPlan.rebase("app", null, true));
        assertThrows(IOException.class, () -> ExportPlan.rebase("team/app", "Bad Org", false));
    }
}
