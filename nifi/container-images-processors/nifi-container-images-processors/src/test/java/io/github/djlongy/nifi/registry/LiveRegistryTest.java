package io.github.djlongy.nifi.registry;

import org.apache.nifi.util.MockFlowFile;
import org.apache.nifi.util.TestRunner;
import org.apache.nifi.util.TestRunners;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

/** Pushes a real export tar to a real registry: LIVE_TAR, LIVE_URL, LIVE_USER, LIVE_PASSWORD, LIVE_REWRITE. */
@EnabledIfEnvironmentVariable(named = "LIVE_URL", matches = ".+")
class LiveRegistryTest {
    @Test
    void push() throws IOException {
        TestRunner runner = TestRunners.newTestRunner(PushContainerImages.class);
        runner.setProperty(PushContainerImages.REGISTRY_URL, System.getenv("LIVE_URL"));
        if (System.getenv("LIVE_USER") != null) {
            runner.setProperty(PushContainerImages.USERNAME, System.getenv("LIVE_USER"));
            runner.setProperty(PushContainerImages.PASSWORD, System.getenv("LIVE_PASSWORD"));
        }
        if (System.getenv("LIVE_REWRITE") != null) {
            runner.setProperty(PushContainerImages.REWRITE_BASE_PATH, System.getenv("LIVE_REWRITE"));
        }
        if ("true".equals(System.getenv("LIVE_STRIP"))) {
            runner.setProperty(PushContainerImages.STRIP_BASE_PATH, "true");
        }
        runner.enqueue(Files.readAllBytes(Path.of(System.getenv("LIVE_TAR"))));
        runner.run();
        for (MockFlowFile f : runner.getFlowFilesForRelationship(PushContainerImages.REL_FAILURE)) {
            System.out.println("FAILURE: " + f.getAttribute("registry.error"));
        }
        for (MockFlowFile f : runner.getFlowFilesForRelationship(PushContainerImages.REL_SUCCESS)) {
            System.out.println("SUCCESS: pushed=" + f.getAttribute("registry.images.pushed") + " present="
                    + f.getAttribute("registry.images.present") + " " + f.getAttribute("registry.images"));
        }
    }
}
