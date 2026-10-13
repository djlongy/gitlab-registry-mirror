package io.github.djlongy.nifi.registry;

import org.apache.nifi.components.state.Scope;
import org.apache.nifi.util.MockFlowFile;
import org.apache.nifi.util.TestRunner;
import org.apache.nifi.util.TestRunners;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.ByteArrayInputStream;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PullContainerImagesTest {

    private PushContainerImagesTest.FakeRegistry low;
    private PushContainerImagesTest.FakeRegistry high;

    @AfterEach
    void stop() {
        for (PushContainerImagesTest.FakeRegistry r : new PushContainerImagesTest.FakeRegistry[]{low, high}) {
            if (r != null) {
                r.server.stop(0);
            }
        }
    }

    /** Fill the low registry with the test export, the way the mirror pipeline's sync would. */
    private void seedLow() throws IOException {
        low = new PushContainerImagesTest.FakeRegistry("bearer");
        low.capture();
        TestRunner push = TestRunners.newTestRunner(PushContainerImages.class);
        push.setProperty(PushContainerImages.REGISTRY_URL, low.url());
        push.setProperty(PushContainerImages.USERNAME, "robot");
        push.setProperty(PushContainerImages.PASSWORD, "secret");
        push.enqueue(PushContainerImagesTest.exportTar());
        push.run();
        push.assertAllFlowFilesTransferred(PushContainerImages.REL_SUCCESS, 1);
    }

    private String digest(String key) {
        return Digests.sha256(low.manifests.get(key));
    }

    private void ledger(String app, String multi) {
        low.ledger = ("{\"registry\":\"" + low.url().substring("http://".length()) + "\",\"synced\":{"
                + "\"team/app:1.0\":{\"digest\":\"" + app + "\",\"tags\":[\"1.0\"]},"
                + "\"team/multi:2.0\":{\"digest\":\"" + multi + "\",\"tags\":[\"2.0\",\"latest\"]}}}").getBytes();
    }

    private TestRunner pull() {
        TestRunner runner = TestRunners.newTestRunner(PullContainerImages.class);
        runner.setProperty(PullContainerImages.REGISTRY_URL, low.url());
        runner.setProperty(PushContainerImages.USERNAME, "robot");
        runner.setProperty(PushContainerImages.PASSWORD, "secret");
        runner.setProperty(PullContainerImages.IMAGE_SOURCE, PullContainerImages.SOURCE_LEDGER);
        runner.setProperty(PullContainerImages.LEDGER_URL, low.url() + "/ledger.json");
        runner.setProperty(PullContainerImages.LEDGER_TOKEN, "read-token");
        runner.setIncomingConnection(false);
        return runner;
    }

    private TestRunner pushHigh(byte[] tar) throws IOException {
        if (high == null) {
            high = new PushContainerImagesTest.FakeRegistry("none");
            high.capture();
        }
        TestRunner push = TestRunners.newTestRunner(PushContainerImages.class);
        push.setProperty(PushContainerImages.REGISTRY_URL, high.url());
        push.enqueue(tar);
        push.run();
        return push;
    }

    @Test
    void exportsTheLedgerDeltaOnceAndTheTarPushesUnchanged() throws IOException {
        seedLow();
        ledger(digest("team/app:1.0"), digest("team/multi:2.0"));
        TestRunner runner = pull();
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_SUCCESS, 1);
        MockFlowFile tar = runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS).get(0);
        assertTrue(tar.getAttribute("filename").matches("container-images-\\d{8}T\\d{6}Z\\.tar"));
        tar.assertAttributeEquals("Filename", tar.getAttribute("filename"));
        tar.assertAttributeEquals("X-Sha256", Digests.sha256(tar.toByteArray()).substring(7));
        tar.assertAttributeEquals("X-Artifact-Type", "container-images");
        tar.assertAttributeEquals("X-Artifact-Format", "tar");
        assertEquals(3, ExportPlan.read(new ByteArrayInputStream(tar.toByteArray())).images.size());

        // The tar goes into another registry with every digest unchanged.
        TestRunner push = pushHigh(tar.toByteArray());
        push.assertAllFlowFilesTransferred(PushContainerImages.REL_SUCCESS, 1);
        for (String key : List.of("team/app:1.0", "team/multi:2.0", "team/multi:latest")) {
            assertEquals(low.manifests.get(key).length, high.manifests.get(key).length, key);
            assertEquals(digest(key), Digests.sha256(high.manifests.get(key)), key);
        }
        for (Map.Entry<String, byte[]> blob : low.blobs.entrySet()) {
            assertTrue(high.blobs.containsKey(blob.getKey()), blob.getKey());
        }

        // Unchanged ledger: nothing new. One record moves: only it is exported.
        runner.clearTransferState();
        runner.run();
        runner.assertTransferCount(PullContainerImages.REL_SUCCESS, 0);
        runner.getStateManager().assertStateEquals("team/app:1.0", digest("team/app:1.0"), Scope.CLUSTER);
        ledger(digest("team/multi:2.0"), digest("team/multi:2.0"));  // app:1.0 now names the multi index
        Map.copyOf(low.manifests).forEach((k, v) -> {
            if (k.startsWith("team/multi:sha256:")) {
                low.manifests.put(k.replace("team/multi:", "team/app:"), v);
            }
        });
        Map.copyOf(low.blobs).forEach((k, v) -> low.blobs.putIfAbsent(k.replace("team/multi@", "team/app@"), v));
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_SUCCESS, 1);
        MockFlowFile delta = runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS).get(0);
        assertEquals("team/app:1.0@" + digest("team/multi:2.0"), delta.getAttribute("registry.images"));
    }

    @Test
    void registryListingNeedsNoLedgerAndHealsAfterLostState() throws IOException {
        seedLow();
        TestRunner runner = pull();
        runner.removeProperty(PullContainerImages.LEDGER_URL);
        runner.setProperty(PullContainerImages.IMAGE_SOURCE, PullContainerImages.SOURCE_LISTING);
        runner.setProperty(PullContainerImages.REPOSITORIES, "team/*");
        low.manifests.put("team/multi:sha256-abc.sig", "{}".getBytes());  // a signature tag: filtered out
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_SUCCESS, 1);
        assertEquals(3, runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS).get(0)
                .getAttribute("registry.images").lines().count());
        runner.clearTransferState();
        runner.run();
        runner.assertTransferCount(PullContainerImages.REL_SUCCESS, 0);
        // A tag gone from the registry leaves the state too, so the state stays the size of the listing.
        low.manifests.remove("team/multi:latest");
        runner.run();
        runner.getStateManager().assertStateNotSet("team/multi:latest", Scope.CLUSTER);
        runner.getStateManager().assertStateEquals("team/multi:2.0", digest("team/multi:2.0"), Scope.CLUSTER);

        // A NiFi rebuilt from scratch with Record Only: it baselines and exports nothing until something changes.
        runner.getStateManager().clear(Scope.CLUSTER);
        runner.setProperty(PullContainerImages.INITIAL_RUN, "Record Only");
        runner.run();
        runner.assertTransferCount(PullContainerImages.REL_SUCCESS, 0);
        runner.getStateManager().assertStateEquals("team/app:1.0", digest("team/app:1.0"), Scope.CLUSTER);
        low.manifests.put("team/app:1.0", low.manifests.get("team/multi:2.0"));  // the tag moves
        Map.copyOf(low.manifests).forEach((k, v) -> {
            if (k.startsWith("team/multi:sha256:")) {
                low.manifests.put(k.replace("team/multi:", "team/app:"), v);
            }
        });
        Map.copyOf(low.blobs).forEach((k, v) -> low.blobs.putIfAbsent(k.replace("team/multi@", "team/app@"), v));
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_SUCCESS, 1);
        assertEquals("team/app:1.0@" + digest("team/multi:2.0"),
                runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS).get(0).getAttribute("registry.images"));
    }

    @Test
    void aDigestMissingFromTheRegistryIsSkippedAndRetried() throws IOException {
        seedLow();
        ledger("sha256:" + "0".repeat(64), digest("team/multi:2.0"));
        TestRunner runner = pull();
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_SUCCESS, 1);
        assertTrue(!runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS).get(0)
                .getAttribute("registry.images").contains("team/app"));
        runner.getStateManager().assertStateNotSet("team/app:1.0", Scope.CLUSTER);
    }

    @Test
    void refusesALedgerForAnotherRegistry() throws IOException {
        seedLow();
        low.ledger = "{\"registry\":\"elsewhere.example.internal\",\"synced\":{}}".getBytes();
        TestRunner runner = pull();
        runner.run();
        runner.assertTransferCount(PullContainerImages.REL_SUCCESS, 0);
        assertTrue(runner.getLogger().getErrorMessages().get(0).getMsg().contains("belongs to elsewhere"));
    }

    @Test
    void exportsARequestOnDemandAndSplitsBySize() throws IOException {
        seedLow();
        TestRunner runner = pull();
        runner.removeProperty(PullContainerImages.LEDGER_URL);
        runner.setProperty(PullContainerImages.IMAGE_SOURCE, PullContainerImages.SOURCE_LISTING);
        runner.setIncomingConnection(true);
        runner.setProperty(PullContainerImages.MAX_SIZE, "1 B");
        runner.setProperty(PushContainerImages.STRIP_BASE_PATH, "true");
        runner.enqueue("team/app:1.0\n# a comment\nteam/multi:2.0\n");
        runner.run();
        runner.assertTransferCount(PullContainerImages.REL_ORIGINAL, 1);
        List<MockFlowFile> tars = runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS);
        assertEquals(2, tars.size());
        assertTrue(tars.get(0).getAttribute("filename").endsWith("-001-of-002.tar"));
        assertEquals("app:1.0@" + digest("team/app:1.0"), tars.get(0).getAttribute("registry.images"));

        // The form registry.images carries: repository:tag@digest, with or without the registry host.
        runner.clearTransferState();
        runner.removeProperty(PullContainerImages.MAX_SIZE);
        runner.enqueue(low.url().substring("http://".length()) + "/team/app:1.0@" + digest("team/app:1.0") + "\n");
        runner.run();
        runner.assertTransferCount(PullContainerImages.REL_SUCCESS, 1);
        assertEquals("app:1.0@" + digest("team/app:1.0"),
                runner.getFlowFilesForRelationship(PullContainerImages.REL_SUCCESS).get(0).getAttribute("registry.images"));

        runner.clearTransferState();
        runner.enqueue("team/app@sha256:abc\n");  // a short digest is refused before any request
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_FAILURE, 1);

        runner.clearTransferState();
        runner.enqueue("team/missing:9.9\n");
        runner.run();
        runner.assertAllFlowFilesTransferred(PullContainerImages.REL_FAILURE, 1);
    }
}
