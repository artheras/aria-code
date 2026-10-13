# Native release assembly

This standard-library-only Go command replaces `scripts/bundle_native_indexer.py`.
It builds the pinned Rust frontend, probes both `--version` and `index imports`,
bundles the indexer inside PyInstaller's contents directory, and optionally
preserves the application worker while installing Rust as the formal launcher.

From the repository root:

```sh
go -C go/buildtools run . --root "$PWD" --frontend "$PWD/dist-native/dist/aria-code-bin"
```

The native release workflows and macOS build script invoke this command before
signing and archiving. It needs Go and Rust at build time; it adds no Go or
Python installation requirement for users of the built distribution. Python
business modules and the application worker remain until their replacements
pass the migration's functional and packaging gates.

`go test ./...` covers fresh and legacy contents layouts, worker preservation,
failed probes, stale-worker refusal, executable permissions and relative Cargo
target directories. Set `ARIA_NATIVE_BINARY` to the built Rust executable for
the actual release-binary test (required in CI).
