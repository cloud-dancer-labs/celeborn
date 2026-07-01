# Homebrew formula for the standalone Celeborn binary.
#
# Lives in the tap repo `cloud-dancer-labs/homebrew-celeborn` as `Formula/celeborn.rb`. Then:
#
#     brew install cloud-dancer-labs/celeborn/celeborn
#
# The release workflow builds per-arch binaries and prints their sha256 (the *.sha256 sidecars);
# bump `version`, the URLs, and the four `sha256` values on each release (the bump can be automated
# from the Release assets). Cask vs. formula: a single-binary CLI is a formula.

class Celeborn < Formula
  desc "Long-term context substrate for coding agents (CLI)"
  homepage "https://github.com/cloud-dancer-labs/celeborn"
  version "0.1.0"
  license :cannot_represent # proprietary — © Thot Technologies LLC

  on_macos do
    on_arm do
      url "https://github.com/cloud-dancer-labs/celeborn/releases/download/v#{version}/celeborn-macos-arm64"
      sha256 "REPLACE_WITH_ARM64_SHA256"
    end
    on_intel do
      url "https://github.com/cloud-dancer-labs/celeborn/releases/download/v#{version}/celeborn-macos-x86_64"
      sha256 "REPLACE_WITH_X86_64_SHA256"
    end
  end

  def install
    # The downloaded asset is the bare binary (named per-arch); install it as `celeborn` + `cel`.
    bin.install Dir["*"].first => "celeborn"
    bin.install_symlink bin/"celeborn" => "cel"
  end

  test do
    assert_match "celeborn", shell_output("#{bin}/celeborn version")
  end
end
