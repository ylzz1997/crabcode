# Client packaging and releases

`.github/workflows/client-packages.yml` builds the VS Code extensions and Desktop
installers from the same commit. It builds Computer Use once on each native
runner, then reuses those bundles for the extensions and installers.

| Trigger | Actions artifacts | GitHub Release assets | Extension stores |
| --- | --- | --- | --- |
| Actions → Client Packages → Run workflow | Yes | No | No |
| Publish a stable GitHub Release | Yes | Yes | Marketplace and Open VSX |
| Publish a prerelease on GitHub | Yes | Yes | No |

The workflow must be committed before creating the release tag. The tag must be
the version in the checked-out client manifests, with an optional `v` prefix
(for example, `0.1.8` or `v0.1.8`). Version mismatches fail before native builds.
Use a new version when changing published packages; existing registry versions
are not replaced. The independent `python-publish.yml` workflow continues to
handle Python/PyPI publishing.

## Release downloads

For version `<version>`, the release receives:

- `crabcode-<version>.vsix`: universal main extension.
- `crabcode-computer-use-<version>-darwin-arm64.vsix`.
- `crabcode-computer-use-<version>-darwin-x64.vsix`.
- `crabcode-computer-use-<version>-win32-x64.vsix`.
- `crabcode-computer-use-<version>-universal.vsix`: all three native bundles.
- `crab-desktop-<version>-darwin-arm64.dmg`: Apple Silicon Mac.
- `crab-desktop-<version>-darwin-x64.dmg`: Intel Mac.
- `crab-desktop-<version>-win32-x64.msi`: Windows x64.

The VSIXs share `vsix-SHA256SUMS`. Each Desktop installer has its own
`desktop-<platform>-SHA256SUMS`, so concurrently finishing jobs cannot overwrite
each other's checksums. Each build uploads its own release assets immediately;
Desktop uploads do not depend on extension-store publication.

Actions retains the packaged downloads for 30 days and intermediate native
bundles for 7 days. Release attachments remain available independently of those
artifact retention periods. Rerunning a release job replaces its same-named
GitHub attachments. Registry publishing skips versions already present, and
the two stores run independently so a failure in one does not cancel the other.

## Trusted publishing

Both registries must authorize repository owner `ylzz1997`, repository `crabcode`,
workflow **`client-packages.yml`**, with **no environment restriction**. Keep the
workflow filename unchanged unless the policies are also updated.

- Visual Studio Marketplace: create a Trusted Publishing policy under publisher
  `crabcode`.
- Open VSX: configure a trusted publisher for each extension, `crabcode` and
  `crabcode-computer-use`, in namespace `crabcode`.

Only the publishing job requests `id-token: write`. It uses Node 22,
`@vscode/vsce@4.1.0` with `--oidc`, and `ovsx@1.3.0` with
`--trusted-publishing`. It verifies artifact SHA256 checksums and publishes the
already-built VSIXs, without rerunning native build or prepublish hooks. No
`VSCE_PAT` or `OVSX_PAT` secret is required. Release uploads use GitHub's built-in
token with `contents: write` in the corresponding build jobs.

See the [Marketplace documentation](https://code.visualstudio.com/api/working-with-extensions/publishing-extension#publish-with-github-actions-and-trusted-publishing)
and [Open VSX CLI documentation](https://github.com/eclipse-openvsx/openvsx/blob/main/cli/README.md#trusted-publishing).

## Verification and signing

The workflow checks synchronized versions, extension icons, client tests,
Computer Use tests, native architectures and runtime metadata. It inspects the
packaged VSIX payloads, executable permissions, localization and icon bytes.
Desktop checks inspect the actual DMG/MSI contents on their native OS runners.
The companion's `logo.png` is a 512-pixel rendition of the native Computer Use
icon; `node packages/computer-use/scripts/build-icons.mjs` regenerates it alongside
the native icons on macOS.

Optional macOS signing secrets are `CRABCODE_SIGNING_CERTIFICATE_P12_BASE64`,
`CRABCODE_SIGNING_CERTIFICATE_PASSWORD`, and `CRABCODE_SIGNING_IDENTITY`.
Without them, macOS builds use ad-hoc signing. The workflow does not notarize
macOS installers or Authenticode-sign Windows installers.

A successful upload is submission evidence. Check Marketplace verification
status and Open VSX public availability separately after the first release run.
