# Homebrew cask for aria-code, served from its own tap:
#
#   brew tap artheras/aria-code https://github.com/artheras/aria-code
#   brew install --cask aria-code
#
# A cask rather than a formula because the release is a prebuilt PyInstaller
# onedir bundle, not something Homebrew compiles. The bundle is staged whole
# and a binary stanza puts `aria-code` on PATH pointing into it — the
# executable loads _internal/ beside itself, so it cannot be relocated alone.
cask "aria-code" do
  version "0.153.0"

  # Measured from the published v0.153.0 archives. Recompute on each
  # release: a stale digest makes brew refuse the download, which is the
  # intended behaviour and not a bug to work around.
  sha256 arm: "da63f32c606c331dfc6194c94e426c63b2fe895224937dc00565fc040701581c",
         intel: "910fe7eb45cf5a9a465ab1566b9360ec45c169148d4e3db82444ba0bbb2d92d4"

  arch arm: "arm64", intel: "x64"

  url "https://github.com/artheras/aria-code/releases/download/v#{version}/aria-code-macos-#{arch}.tar.gz"
  name "aria-code"
  desc "AI agent for coding, financial analysis and logistics operations"
  homepage "https://github.com/artheras/aria-code"

  livecheck do
    url :url
    strategy :github_latest
  end

  binary "aria-code-bin/aria-code-bin", target: "aria-code"

  # The bundle carries its own interpreter and writes nothing outside the
  # user's config, so only that is left to clean up.
  zap trash: [
    "~/.arthera",
    "~/Library/Caches/aria-code",
  ]
end
