package io.github.djlongy.nifi.registry;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

final class Digests {

    private Digests() {
    }

    static MessageDigest sha256() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static String sha256(byte[] data) {
        return "sha256:" + HexFormat.of().formatHex(sha256().digest(data));
    }
}
