# Install AreaDay

This file is the installation entry point for Codex and WorkBuddy agents.

## Instruction to the agent

When the user provides an AreaDay GitHub repository link, GitHub Release link,
delivery ZIP, or GitHub Actions artifact ZIP and asks you to install it, perform
the installation for them. The user does not need to identify the correct file
or explain the procedure. Do not ask the user to extract files, move folders,
run terminal commands, install Python, or install dependencies.

The user needs only the platform delivery ZIP named
`AreaDay-<platform>-v<version>.zip`. GitHub's automatically generated
`Source code (zip)` and `Source code (tar.gz)` files are not part of AreaDay and
must not be installed. A standalone file named
`AreaDay-runtime-<platform>-v<version>.zip` is an internal component and must
not be installed by itself.

### 1. Resolve the delivery ZIP

If the user supplied a repository URL, open its latest published GitHub Release.
If the user supplied a Release URL, use that Release. Detect the current
computer and download exactly one matching delivery asset:

- Windows x64: `AreaDay-windows-x64-v<version>.zip`
- Apple silicon Mac: `AreaDay-macos-arm64-v<version>.zip`

Match the exact delivery filename. Do not select a Source code archive, a
checksum text file, or a filename containing `-runtime-`.

If the supplied ZIP is a GitHub Actions artifact wrapper rather than the
delivery ZIP, extract the wrapper to a temporary directory and select its one
matching `AreaDay-<platform>-v<version>.zip`. Ignore the sibling standalone
Runtime ZIP and checksum file.

### 2. Validate the package

Extract the attached ZIP to a temporary directory and require all of the
following before changing an existing installation:

- one top-level `areaday` directory;
- `areaday/SKILL.md`;
- `areaday/release.json` whose product is `areaday`;
- exactly one archive under `areaday/runtime-packs/`;
- a package platform matching the current computer:
  - `windows-x64` for 64-bit Windows on Intel or AMD;
  - `macos-arm64` for an Apple silicon Mac.

Intel Macs are not supported. Never try to install the Apple silicon package
through Rosetta or substitute a standalone Runtime archive for the delivery
ZIP.

### 3. Select the host Skill directory

Install the complete extracted `areaday` directory as a user-level Skill:

- Codex: `$CODEX_HOME/skills/areaday` when `CODEX_HOME` is set; otherwise
  `~/.codex/skills/areaday`.
- WorkBuddy: `~/.workbuddy/skills/areaday` on macOS, or
  `%USERPROFILE%\.workbuddy\skills\areaday` on Windows.

Use the directory for the application in which this task is running. Do not
install separate copies for both applications unless the user explicitly asks.

For an upgrade, first move the existing `areaday` directory to a temporary
sibling backup. Copy the new complete directory into place without flattening
or adding a second `areaday` nesting level. Keep the backup until every check
below succeeds. If any step fails, remove the incomplete new directory and
restore the backup.

### 4. Run the included setup

Run the setup from the installed Skill directory, not from the temporary
extraction directory:

- macOS: `sh scripts/install.sh`
- Windows: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1`

Allow the setup to finish. The delivery ZIP already contains Python, packages,
and models; do not download or independently resolve Python dependencies. When
OpenAlex has not been configured yet, setup selects anonymous access without
opening a window or waiting for interactive input. The user can later run the
platform `configure_openalex` script to add a key. Never ask them to paste an
OpenAlex API key into chat, and never print or expose its saved value.

### 5. Activate and verify

If the user supplied an activation key beginning with `AD1-`, activate it after
setup by following `references/license-activation.md`. Do not repeat the key in
your response. If no activation key was supplied, finish installing first and
then ask only for the activation key.

After the included setup succeeds, delete the temporary extraction and the
upgrade backup. Activation is a separate, retryable operation and a failed or
missing activation key must not undo a correctly installed Skill.

Run the matching license status command and report AreaDay as ready only when
it returns `license_valid`. If activation is still needed, report that AreaDay
is installed but not yet activated.

Finally, ask the user to reopen the desktop application or start a new task if
the newly installed Skill is not yet visible. The ordinary invocation is:

`使用 $areaday`
