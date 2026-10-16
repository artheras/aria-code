# Native release assembly

This standard-library-only Go command replaces `scripts/bundle_native_indexer.py`.
It builds the pinned Rust frontend, probes both `--version` and `index imports`,
bundles the indexer inside PyInstaller's contents directory, and optionally
preserves the application worker while installing Rust as the formal launcher.

From the repository root:

```sh
go -C go/buildtools run . --root "$PWD" --frontend "$PWD/dist-native/dist/aria-code-bin"
go -C go/buildtools run . verify "$PWD/dist-native/dist/aria-code-bin"
go -C go/buildtools run . pack "$PWD/dist-native/dist/aria-code-bin" "$PWD/dist-native/release"
go -C go/buildtools run . unpack --dereference "$PWD/dist-native/release/aria-code-bin.tar.gz" "$PWD/unpacked"
```

`pack` and `unpack` replace the old `package_onedir.py` archive tool. TAR retains
relative symlinks and executable modes; ZIP (the Windows default, or `--zip`)
rejects symlinks. Extraction stages every entry before replacing a destination,
rejects traversal, devices, duplicate paths, external/dangling links and link
parents, and caps the extracted tree at 100,000 entries and 8 GiB. Dereferencing
also checks directory cycles and applies the same size/count budget to copies.
A failed archive build or extraction preserves the previous output.

`packages` replaces `make_platform_packages.py`. Build the command once to use
relative paths from the repository root:

```sh
go -C go/buildtools build -o /tmp/aria-bundle .
/tmp/aria-bundle packages --version 0.152.0 --out npm/platforms \
  --binary darwin-arm64=built/aria-code-macos-arm64/aria-code-bin.tar.gz \
  --mcp darwin-arm64=built/aria-code-mcp-macos-arm64/aria-code-mcp-bin.tar.gz
```

Repeat `--binary`/`--mcp` for each platform. The sets must match, each archive
must contain the expected executable, and every input is validated before any
existing package is replaced. CLI and MCP remain separate npm packages, with
the existing `os`, `cpu`, license, executable names and `bin/` structure.
Internal links are copied into real files/directories for npm. Directory
promotion is atomic per package, not a crash transaction across all packages.
The release workflow requires all five platform pairs and its actual shell
assembly step is tested against ten fixture archives and the compiled Go tool.

The native release workflows and macOS build script invoke this command before
signing and archiving. It needs Go and Rust at build time; it adds no Go or
Python installation requirement for users of the built distribution. Python
business modules and the application worker remain until their replacements
pass the migration's functional and packaging gates.

`verify` replaces the former Python frontend smoke-test script. It launches
the real packaged frontend, worker and helper; checks version agreement,
Unix command symlinks, product help, the application handshake, robot rows,
orderly shutdown and actual file contents returned by the read tool. User
state is isolated in a temporary directory, runtime overrides are removed,
and no model request is sent. Commands, protocol frames and diagnostic output
are bounded. Both current `_internal/` and legacy flat layouts are supported.
All paths passed after `go -C` must be absolute (Go changes working directory).

Set `ARIA_RELEASE_DIR` to a frozen distribution to run the verifier's actual
release test. Ordinary tests use real child executables to exercise startup,
shutdown, malformed/oversized output, version mismatch, timeouts and incorrect
read results. The native release workflow runs `verify` on its actual builds
before archiving them.

`go test ./...` covers fresh and legacy contents layouts, worker preservation,
failed probes, stale-worker refusal, executable permissions and relative Cargo
target directories. Set `ARIA_NATIVE_BINARY` to the built Rust executable for
the actual release-binary test (required in CI).
