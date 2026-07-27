package app;

import java.nio.file.Files;
import java.nio.file.Path;

import org.ofdrw.converter.ConvertHelper;

/** Deterministically export an OFD fixed layout to PDF through OFDRW. */
public final class OFDRenderer {
    private OFDRenderer() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("usage: OFDRenderer <input.ofd> <output.pdf>");
        }
        Path input = Path.of(args[0]);
        Path output = Path.of(args[1]);
        if (!Files.isRegularFile(input)) {
            throw new IllegalArgumentException("input OFD does not exist");
        }
        Path parent = output.toAbsolutePath().getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }

        // Preserve the original OFD fixed layout; no OCR, raster enhancement, or regeneration.
        ConvertHelper.toPdf(input, output);
    }
}
