---
description: "Show the device code, activate or inspect a development license, or preflight personal-vocabulary prediction access."
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
.venv/bin/python scripts/prediction_preflight.py
```

On Windows, use `.venv\Scripts\python.exe` instead. For `activate`, use only the
activation key the user explicitly supplied. The command sends the activation
key and current device code to the configured development service, validates
the returned signature, and atomically installs the license. Never ask the user
to locate or move the resulting license file.

The default development endpoint is the isolated HTTPS Cloudflare Worker at
`https://license-dev.areaday.app`. A connection failure may return
`activation_service_unavailable`. Do not weaken TLS, use an unofficial proxy,
or treat that network error as a bad key.

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

Creating a new personalized vocabulary is a separate licensed online operation.
After the local mini corpus exists, the workbench sends only compact word
statistics and isolated-word answers to the same development endpoint. It never
sends PDFs, extracted text, sentences, source papers, URLs, or local paths.
`calibration_service_unavailable` means that the prediction service could not be
reached; it does not mean the installed license is invalid. A previously
completed local result remains viewable while the service is unavailable.

Use `prediction_preflight.py` at the beginning of a request to establish, build,
or rebuild a personal vocabulary. It sends only the installed license envelope
and returns one of these stable results:

- `prediction_ready`: the local license and server authorization both passed.
- `license_required`: stop before profile review or paper collection and offer
  the matching activation action.
- `prediction_service_unavailable`: explain that paper collection may continue,
  but calibration and the personal vocabulary cannot finish while the service
  remains unavailable; ask whether the user wants to continue or postpone.
- `prediction_service_error`: stop and report the exact code without calling it
  a bad license.

The four public ResearchRamp business entrypoints are development-license
gated. The familiarity-prediction core is now server-side, so changing the local
gate cannot reproduce that protected function. This development service is not
the production sales environment and must not be described as one.
