---
description: "Show the local ResearchRamp device code, activate with a development key, install a recovery license, or inspect license status."
---

# ResearchRamp development-license activation

This reference applies only to the isolated licensing preview. The Cloudflare
development activation service, signing key, D1 database, product identifier,
and application-data directory are separate from the future production license
environment.

Use one command from the Skill directory:

```bash
.venv/bin/python scripts/researchramp_license.py device-id
.venv/bin/python scripts/researchramp_license.py activate <activation-key>
.venv/bin/python scripts/researchramp_license.py install <absolute-rrlicense-path>
.venv/bin/python scripts/researchramp_license.py status
```

On Windows, use `.venv\Scripts\python.exe` instead. For `activate`, use only the
activation key the user explicitly supplied. The command sends the activation
key and current device code to the configured development service, validates
the returned signature, and atomically installs the license. Never ask the user
to locate or move the resulting license file.

The default development endpoint is the isolated HTTPS Cloudflare Worker. A
network that blocks or incorrectly resolves `workers.dev` may return
`activation_service_unavailable`. Do not weaken TLS, use an unofficial proxy,
or treat that network error as a bad key. A future production endpoint will use
a separately owned domain.

`install` remains a recovery and compatibility operation. Use only the absolute
path of the `.rrlicense` file the user explicitly attached or named. Do not
search Downloads, Desktop, another conversation, or the filesystem for a
license.

If the Skill-local runtime is absent, use the existing platform installer and
verify it before running a license command. Do not ask the user to manually
install the `cryptography` dependency.

The command performs the complete operation. Both `activate` and `install`
verify the envelope, Ed25519 signature, product, major version, device count,
and current device before atomically writing the license. A failed activation
must not replace an already valid local license. Never manually copy, rewrite,
repair, or construct a license file.

Report the command's stable result:

- `device_ready`: give the user the exact device code to send to the seller.
- `license_activated`: confirm the development license ID and report the device
  slots returned by the service.
- `license_installed`: confirm the development license ID and licensed customer.
- `license_valid`: confirm the installed development license is valid on this
  computer.
- `license_error`: explain its `code` and `error` without treating it as a
  network failure.

An activation-service connection failure affects only new activation. It does
not invalidate a license that is already installed, and it must not be reported
as an invalid local license.

This milestone does not authorize claiming that ResearchRamp's research
features are license-gated, that the Cloudflare development service is the
production service, or that the protected core has been compiled. Those belong
to later milestones.
