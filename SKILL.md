---
name: researchramp
description: "Build a local research area and personal domain vocabulary, open the ResearchRamp workbench for vocabulary, briefs, review, and domain switching, or configure automatic weekly briefs."
---

# ResearchRamp

ResearchRamp has three user-facing capability groups:

- **首次建立**: establish a confirmed research area and build its personal
  domain vocabulary and terminology from a local research corpus.
- **日常使用**: open one unified workbench. Inside it, the user can:
  - 查看领域词表；
  - 查看本周或往期研究简报；
  - 复习生词与术语；
  - 自己切换研究领域。
- **持续更新**: configure automatic weekly research briefs for selected
  initialized domains.

**打开工作台** is the single ordinary entry point for daily use. It does not
replace the separate product capability to establish a research area and build
its personal domain vocabulary.

When the user asks what ResearchRamp or this Skill can do, answer with concise
bullet points rather than one compressed paragraph. Use this structure:

> 我可以帮你：
>
> - **建立研究领域与个人领域词表**：围绕你确认的研究方向收集论文，生成个人化的领域生词和术语。
> - **打开研究工作台**：在一个界面里查看领域词表和研究简报、复习生词与术语，并自行切换研究领域。
> - **开启自动每周研究简报**：按照设定的时间，为选定的研究领域持续生成简报。

Keep vocabulary viewing, brief viewing, review, and domain switching grouped as
things the user does inside the workbench, not separate agent-operated commands.
Do not hide initialization behind the vague phrase "建立工作台"; explicitly say
that ResearchRamp can establish a research area and build a personal domain
vocabulary.

## Open the workbench: fast path

Requests to open ResearchRamp, view vocabulary or briefs, review words or terms,
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

The launcher performs the operational work: it identifies a compatible live
ResearchRamp service on port 8765 by its exact registry, starts the
registered-domain service only when needed, waits until it is ready, and prints
the exact user-facing URL. Do not repeat runtime checks, registry inspection,
server startup, identity checks, or URL construction outside the launcher.

Each Skill instance owns exactly one registry at
`<skill-root>/researchramp-data/real-domains.json`. The current-path Skill and
the installed Skill therefore have separate registries; never redirect either
one to Home and never fall back to another Skill instance's registry. Each
entry stores the domain ID, display name, and the absolute path of the actual
user-confirmed workspace. That workspace—and its papers—may be anywhere on the
filesystem. Do not discover domains by scanning or import entries from another
registry. Global learning state lives beside this instance's registry.

Open the returned URL through the host's direct page-opening capability and
then stop. Do not inspect the page, take control of the page, click a tab or
button, start a review session, reload it, or test the interface unless the user
explicitly asks for diagnosis or UI testing. The user operates the workbench.

The exact URL must contain both the selected domain and landing view:

```text
http://127.0.0.1:8765/?domain=<domain-id>#<view>
```

## First-time initialization

If no registered domain exists, explain that the first research area must be
initialized before the workbench can open. For `$researchramp init` or an
equivalent request, read [full-workflow.md](references/full-workflow.md) and
follow its initialization sections. As soon as the user has confirmed the
research area and its exact workspace path, save the confirmed profile there
and immediately register that absolute path in this Skill instance's registry.
Do this before discovery, downloads, vocabulary generation, or calibration; the
first corpus command also verifies the same registration idempotently before it
does any research work. Calibration writes only personal-vocabulary artifacts
inside the registered workspace and must never create, remove, or roll back a
domain registration. Do not ask the user to run the registration command. Do
not read that long reference for an ordinary workbench-opening request.

## Automatic weekly briefs

Weekly briefs are scheduled artifacts, not one-off manual reports. If the user
wants to enable or change them, open the workbench's schedule view and follow
the scheduling handoff in
[continuous-workflow.md](references/continuous-workflow.md). With multiple
domains, schedule each selected domain independently. Do not read the
continuous workflow for ordinary viewing or review.

All corpora, vocabulary, briefs, settings, and learning records remain in the
user-confirmed local ResearchRamp directories.
