# Native document processing validation

Development worktree only; no installed command replacement or cloud deployment.

## Local OCR setup

Install Poppler and Tesseract on the runtime host. Put `eng.traineddata`,
`chi_sim.traineddata` and `chi_tra.traineddata` in `build/native-dev/tessdata`.
The dev launcher sets `ARIA_OCR_DATA` to this directory when present. The local
test data was downloaded from the official `tesseract-ocr/tessdata_fast` repository
at commit `87416418657359cb625c412a48b6e1d6d41c29bd`; files are build artifacts,
not vendored runtime assets. See https://github.com/tesseract-ocr/tessdata_fast
and https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html.

```
/config set document_ocr=true
/config set ocr_language=eng+chi_sim+chi_tra
/config set document_vision=true
Analyze @file:report.pdf
/export bundle diagnostics.json
```

Vision opt-in sends images to the currently selected provider; choose a vision
model. Tests verify payload/history preservation with a fixture provider, not
the correctness of an external model's chart interpretation. Embedded DOCX/PPTX
charts are not rendered. OCR only fills pages without a text layer; a partially
scanned page with existing text needs vision or manual preprocessing.

## Repeatable checks

On macOS generate a synthetic Chinese/English test image:

```
swift scripts/ocr_test_fixture.swift build/native-dev/ocr-fixture.png
cd go/runtime
ARIA_OCR_DATA="$PWD/../../build/native-dev/tessdata" \
ARIA_OCR_TEST_IMAGE="$PWD/../../build/native-dev/ocr-fixture.png" \
go test -race ./...
```

The OCR test embeds this raster into a textless PDF, runs page rendering and
multilingual OCR, and checks recognizable simplified/traditional Chinese and
numeric evidence. It is not an accuracy benchmark: character substitutions and
spacing errors are expected and must not be presented as verified source values.
Without the environment variables, multilingual integration is explicitly skipped.

macOS tests check private-file reads, out-of-scratch writes and network denial,
plus successful sandboxed extraction. Linux/Windows restricted conversion is
still fail-closed; those platforms need separate adapters. Dependency directories
are readable, and subprocess output/time limits do not provide a hard memory quota.

Diagnostics store only lifecycle metadata, not raw prompts, tool output, private
paths or credentials. The bounded trace is not an exhaustive forensic log or a
replacement for provider/MCP health checks.
