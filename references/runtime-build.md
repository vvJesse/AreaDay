---
description: "Build and deliver the Windows x64 and Apple silicon AreaDay runtime bundles with GitHub Actions."
---

# Build AreaDay runtime bundles

AreaDay has two supported native runtime targets because Python and compiled
packages are different on each operating system and processor:

| Runtime | GitHub runner | Customer computer |
| --- | --- | --- |
| `windows-x64` | `windows-2025` | 64-bit Windows on Intel/AMD |
| `macos-arm64` | `macos-15` | Apple silicon Mac |

Intel Macs are not a release target. Support can be evaluated separately when
there is concrete customer demand, without delaying the supported releases.

The workflow in `.github/workflows/build-runtimes.yml` builds both targets in
parallel. Each runner creates a relocatable Python 3.12 environment, installs
the frozen dependencies, embeds the Python interpreter itself, and downloads
the pinned unquantized ONNX embedding model and tokenizer. ONNX Runtime
telemetry is disabled. The build directory is then deleted; the workflow
extracts the new Runtime ZIP into an unrelated directory and runs a real
offline inference check. It then puts that verified runtime inside the matching
customer ZIP and tests the customer ZIP through the installer's `runtime-only`
path.

## Run a build

1. Commit and push the runtime files and workflow to GitHub.
2. Open the repository's **Actions** page.
3. Select **Build AreaDay runtimes**.
4. Select **Run workflow**, confirm version `1.0.4`, and start it.
5. Wait for both jobs to become green.
6. Download the two artifacts from the completed workflow run.

Each GitHub artifact contains:

- `AreaDay-<platform>-v<version>.zip`: the file to give the customer.
- `SHA256SUMS-<platform>.txt`: hashes for checking that the downloads are intact.

The standalone Runtime remains inside the customer ZIP and is not uploaded a
second time. This keeps the downloadable Artifact close to half the former
size. Give each customer only the AreaDay ZIP matching their computer,
together with their separately issued `AD1-...` activation key.

To publish without downloading these large files locally, run **Publish
verified AreaDay release** from `main`, enter the successful Runtime workflow
run ID and version, and let GitHub transfer the verified customer ZIPs directly
to the Release.

## When to rebuild

Run the workflow when AreaDay code changes for a release, when a dependency or
model changes, or when the Python/runtime build logic changes. A new runtime is
not needed for every customer or every activation key.

The runtime dependency source is `pyproject.toml`; `uv.lock` freezes the
complete resolved dependency graph and the hashes of every platform artifact.
If dependencies are intentionally changed, regenerate `uv.lock` with the
pinned uv version before running the workflow.
