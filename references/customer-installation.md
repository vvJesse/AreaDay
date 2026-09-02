---
description: "Install or upgrade the AreaDay Skill on macOS or Windows x64, then activate it with the customer's activation key."
---

# Install AreaDay

The customer receives two separate items: one platform-specific AreaDay ZIP
and one activation-key string beginning with `AD1-`. Choose exactly one ZIP:

| Customer computer | Delivery file |
| --- | --- |
| Windows 64-bit | `AreaDay-windows-x64-v<version>.zip` |
| Apple silicon Mac (M1 or newer) | `AreaDay-macos-arm64-v<version>.zip` |

Intel Macs are not currently supported. Do not give an Apple silicon package
to an Intel Mac customer; handle such requests separately.

The ZIP already contains Python, all Python packages, the spaCy model, and the
embedding model for that operating system. The first setup therefore verifies
and installs the included runtime instead of downloading those dependencies.
It is a Codex Skill bundle, not a desktop application installer. The customer
never receives or moves a `.rrlicense` file during ordinary activation.

## macOS

Extract the ZIP so the resulting folder is `~/.codex/skills/areaday`, then run:

```bash
sh ~/.codex/skills/areaday/scripts/install.sh
```

## Windows x64

Extract the ZIP so the resulting folder is
`%USERPROFILE%\.codex\skills\areaday`, then run in PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.codex\skills\areaday\scripts\install.ps1"
```

The setup keeps research workspaces outside the Skill bundle. It also copies
the exact former sibling `researchramp\researchramp-data` directory on the first
AreaDay installation when legacy data exists; it never deletes the source.
Upgrading the Skill therefore does not consume a new device slot or erase an
existing registry.

The installer retains the former online setup as a compatibility fallback for
old, platform-neutral bundles. New platform bundles use the included runtime.

After setup, reopen Codex if AreaDay is not yet listed, then invoke `$areaday`
and supply the separately received activation key. AreaDay contacts
`https://license.areaday.app`, validates the signed response, and installs the
license automatically in the operating system's application-data directory.
