---
name: areaday
display_name: AreaDay
display_name_en: AreaDay
description: "Build a local research area and personal domain vocabulary, view or generate research briefs, open the AreaDay workbench, or schedule weekly briefs and daily review reminders."
description_zh: "建立本地研究领域与个人词表，查看研究简报并进行复习。"
description_en: "Build a local research area and personal vocabulary, read briefs, and review terminology."
category: education
version: 1.0.4
author: AreaDay
---

# AreaDay

## Production license gate

AreaDay uses a production license. A valid
installed license is required before initialization, workbench launch, brief
generation, or schedule configuration can perform business work. These public
entrypoints all enforce the same offline verifier before reading or changing a
research workspace.

Treat requests to show the AreaDay device code, activate AreaDay,
install an explicitly supplied recovery `.rrlicense` file, or inspect the
installed license as license operations. Read
[license-activation.md](references/license-activation.md), then run exactly the
matching command from that reference. The ordinary activation flow accepts an
activation key and automatically installs the returned license; the user never
manually copies a license into an application-data directory.

For installation or upgrade requests, read
[INSTALL.md](INSTALL.md) completely, then perform its agent installation
protocol. Use [customer-installation.md](references/customer-installation.md)
only as supplementary customer-delivery context. Never describe the ZIP as a
desktop App or ask the user to perform terminal installation steps for you.

Commands in this file use `.venv/bin/python` for macOS. On Windows, always use
`.\.venv\Scripts\python.exe` in its place and keep every following argument
unchanged. Never try to execute the macOS `.venv/bin/python` path on Windows.

When a business entrypoint returns `license_required`, report its exact `code`
and `error`, then offer the matching device-code, activation, status, or recovery
installation operation. Do not retry the business operation through a helper
script or another entrypoint. Ordinary gate checking is offline. Creating a new
personalized vocabulary uses the licensed AreaDay prediction service after the
mini corpus is complete; an unreachable predictor must not be reported as an
invalid license.

At the start of every concrete business request—including establishing,
building, or rebuilding a domain—run the local offline check exactly once,
before asking product-specific follow-up questions or reading a workspace:

```bash
.venv/bin/python scripts/researchramp_license.py status
```

On `license_valid`, continue. On `license_error`, explain it and offer the
matching activation operation before doing other product work. This preliminary
license check must not contact the remote prediction service or test whether it
is currently reachable. During first-time setup, that service is contacted only
after the local corpus, vocabulary, and terminology are ready and the user is
about to begin the 30-question calibration.

Do not repeat the conversational precheck during one uninterrupted operation;
the public scripts still enforce the local license again at their actual
side-effect boundary.

AreaDay has four user-facing capability groups:

- **首次建立**: establish a confirmed research area and build its personal
  domain vocabulary and terminology from a local research corpus.
- **日常使用**: open one unified workbench. Inside it, the user can:
  - 查看领域词表；
  - 查看最新或往期研究简报；
  - 复习生词与术语；
  - 自己切换研究领域。
- **研究简报**: view existing briefs, generate one brief now, or schedule weekly
  generation for an initialized domain.
- **复习提醒**: independently schedule a daily reminder when words or terms are due.

**打开工作台** is the single ordinary entry point for daily use. It does not
replace the separate product capability to establish a research area and build
its personal domain vocabulary.

When the user asks what AreaDay or this Skill can do, answer with concise
bullet points rather than one compressed paragraph. Use this structure:

> 我可以帮你：
>
> - **建立研究领域与个人领域词表**：围绕你确认的研究方向收集论文，生成个人化的领域生词和术语。
> - **打开研究工作台**：在一个界面里查看领域词表和研究简报、复习生词与术语，并自行切换研究领域。
> - **生成研究简报**：立即围绕选定领域生成并保存一份新的研究简报。
> - **开启自动每周研究简报**：按照设定的时间，为选定的研究领域持续生成简报。
> - **设置每日复习提醒**：只在有到期生词或术语时提醒复习。

Keep vocabulary viewing, brief viewing, review, and domain switching grouped as
things the user does inside the workbench, not separate agent-operated commands.
Do not hide initialization behind the vague phrase "建立工作台"; explicitly say
that AreaDay can establish a research area and build a personal domain
vocabulary.

## Open the workbench: fast path

Requests to open AreaDay, view vocabulary or briefs, review words or terms,
or switch domains all use this fast path. A specific request may choose the
matching landing view, but it remains the same workbench.

From this Skill directory, run exactly one launcher command:

```bash
.venv/bin/python scripts/open_workbench.py --view <vocabulary|briefs|review>
```

Opening the workbench writes learning state beside this instance's registry and
inside its registered workspaces. When those paths are outside the current
task's writable roots, obtain host filesystem permission before running this
single launcher command. Do not first run it in a restricted sandbox and then
retry: a startup failure must be reported from that one invocation with its
original cause.

Add `--domain <registered-domain-id>` only when the user explicitly named a
domain. Otherwise let the launcher reuse the running workbench or select the
registry's active domain. Do not infer authorization from an ambient browser
tab.

The launcher performs the operational work: it treats port 8765 as the preferred
port, identifies a compatible live AreaDay service across its bounded fallback
range by exact registry, starts the registered-domain service only when needed,
waits until it is ready, and prints the exact user-facing URL. Do not repeat
runtime checks, registry inspection, server startup, identity checks, port
selection, or URL construction outside the launcher.

The production Skill uses one upgrade-safe AreaDay registry in the operating
system's application-data directory. The installer performs one exact legacy
migration from the former sibling `researchramp/researchramp-data` directory;
it never scans for or imports unrelated workspaces. Each registry entry stores
the domain ID, display name, and the absolute path of the actual user-confirmed
workspace. That workspace—and its papers—may be anywhere on the filesystem.
Global learning state lives beside the registry.

Open the returned URL through the host's direct page-opening capability and
then stop. Do not inspect the page, take control of the page, click a tab or
button, start a review session, reload it, or test the interface unless the user
explicitly asks for diagnosis or UI testing. The user operates the workbench.

The exact returned URL must contain the launcher-selected port, selected domain,
and landing view. Port 8765 remains preferred; a nearby fallback may be used
when it is unavailable:

```text
http://127.0.0.1:<selected-port>/?domain=<domain-id>#<view>
```

## First-time initialization

If no registered domain exists, explain that the first research area must be
initialized before the workbench can open. Treat any request to establish a
research area or build or rebuild its personal domain vocabulary—including
“构建词表”—as equivalent to `$areaday init`: read
[full-workflow.md](references/full-workflow.md) completely.

Treat everything after the user confirms the profile and workspace—discovery,
candidate review, PDF acquisition, corpus analysis, orthography review,
terminology review, joint finalization, and verified workbench startup—as **one
unattended preparation operation**. Run it only through
`scripts/initialize.py`; do not present its internal checkpoints as independent
stages and do not use the helper commands as the normal workflow.

The controller's `status.json` is authoritative. While `terminal` is `false`,
the current task must continue:

- wait on the same live command when it is still running;
- perform any `next_action` whose actor is `current_host_agent`, write the exact
  requested output, and immediately run the supplied `resume` command;
- diagnose a real failure from preserved artifacts and resume the same
  operation.

For a vocabulary-card review action, the review records are always in the
input file's top-level `.candidates` array; `vocabulary_card_review_batch` is
only the controller's label for that input path, not a JSON field. Check
`.candidate_count == (.candidates | length)` before reviewing. Work through a
large review only through the bounded batch supplied on each controller resume,
preserving prior glosses and drops, and make a corpus-grounded sense decision
for each item. Never generate the review by taking the first dictionary meaning
or by filling fields merely to make finalization continue. For each candidate,
either write a contextual gloss with a stable sense key and a brief rationale,
or put an uncertain or noisy lemma in `vocabulary_card_drops`. A
unique `acronym_expansion` controls the exact English meaning and suggested
`sense_key`; it overrides a conflicting dictionary abbreviation. If exhaustive
semantic review is not complete, do not write a nominally complete selection
and do not resume finalization.

An internal command exit, a candidate count, downloaded PDFs, completed corpus
analysis, a vocabulary file, or a terminology count is never a reason to stop.
The controller must finish the standalone exhaustive orthography review before
preparing any vocabulary-card gloss candidates. It then runs
`finalize_domain_assets.py`, which loads the already finalized vocabulary and
finalizes its cards together with terminology. `ready_for_calibration` is a
pipeline-readiness signal, not an independent claim that arbitrary prose is
correct; it is valid only after the vocabulary-card semantic review contract
and the asset loaders both succeed. It may start calibration only after that
invocation succeeds.
The preparation operation may hand control to the user only when the controller
returns `terminal: true`, `checkpoint: calibration_service_ready`, and a live
URL whose service record says both `vocabulary_ready` and
`terminology_ready`. Open that exact URL and ask the user to answer the 30
calibration questions. Do not claim that full initialization is complete until
those answers and the personalized export have been verified.

Start by checking the Skill-local runtime. If dependencies are missing,
proactively request the required network and disk-write permission; after
approval, run the platform installer yourself and verify it before continuing.
Do not ask the user to configure Python dependencies manually. Do not read the
long workflow reference for an ordinary workbench-opening request.

## Research briefs and reminders

Keep these four requests distinct:

For both immediate brief generation and weekly brief scheduling, resolve the
domain before doing any work:

- If the user named a registered domain, use it.
- If the user did not name a domain and exactly one initialized domain is
  registered, use that domain without asking.
- If several domains are registered and the user did not name one, ask the user
  to choose from the registered domains. Do not start generation, save schedule
  settings, or create a task before the choice is made.
- If the named domain is not registered, show the registered choices and ask
  again. Never fall back to the active domain, an open workbench tab, or another
  Skill instance's registry.

Once a weekly task is created, bind it to the resolved domain ID and workspace;
scheduled runs do not ask again.

- **View briefs**: use the ordinary workbench fast path with `--view briefs`.
  Opening the page never starts research or creates a schedule. If no brief
  exists, tell the user that none has been generated and that Codex / Work Buddy
  can generate one.
- **Generate a brief**: read
  [continuous-workflow.md](references/continuous-workflow.md), then run the
  single controller below after applying the domain rule above.

  ```bash
  .venv/bin/python scripts/generate_brief.py run [--domain <domain-id>]
  ```

  While its `terminal` value is `false`, perform the supplied host-agent
  `next_action` and immediately run its exact `resume` command. Discovery,
  selection, download, preparation, and draft writing are never completion
  points. When it returns `terminal: true` with `generated: true`, open the
  workbench at `--view briefs`. When it returns `generated: false`, report that
  no brief was saved and give the controller's reason.
- **Schedule a weekly brief**: read only the scheduling section of the same
  reference. Resolve the domain using the same rule, obtain the weekday and
  time, run `configure_schedule.py weekly`, then create or update exactly the
  one host scheduled task emitted in its handoff. The task runs the same
  brief-generation controller. Do not generate a brief immediately unless the
  user also asked for one.
- **Schedule a daily review reminder**: obtain the time, run
  `configure_schedule.py daily`, then create or update exactly the one reminder
  task emitted in its handoff. It only checks due words and terms. It never
  generates a brief or changes the weekly task.

Never treat a research brief as necessarily weekly. Repeated scheduling updates
the task with the same `automation_key`; it must not create duplicates. Do not
read the continuous workflow for ordinary viewing or review.

PDFs, extracted text, source metadata, corpora, briefs, settings, and learning
records remain in the user-confirmed local AreaDay directories. During a
new 30-question vocabulary calibration, the licensed predictor receives only
the compact word statistics and isolated-word answers defined in
`references/vocabulary-calibration.md`. The final result is saved locally and
can be viewed later without contacting the predictor.
