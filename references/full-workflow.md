---
name: researchramp-full-workflow
description: "Prepare one confirmed ResearchRamp domain, launch its verified calibration workbench, and finish the personalized vocabulary after the user's answers."
---

# ResearchRamp first-time workflow

Use this reference only when establishing or rebuilding a research domain. An
ordinary request to open vocabulary, briefs, review, or domain switching uses
the fast path in `SKILL.md`.

## The lifecycle contract

There is one uninterrupted preparation operation:

```text
confirmed profile and workspace
        ↓
unattended research and host-agent review
        ↓
vocabulary + terminology finalized together
        ↓
registered library service started and live-verified
        ↓
user receives the 30-word calibration page
```

Discovery, candidate review, downloads, analysis, orthography review, and
terminology review are implementation checkpoints inside that operation. Never
report one of them as the outcome, never ask the user to return merely because a
checkpoint finished, and never interpret a helper process exit as permission to
stop. The user may leave the task while the host agent continues.

`<workspace>/status.json` is the only lifecycle record. Its meaning is strict:

- `terminal: false`: this task is still responsible for continuing. If
  `next_action.actor` is `current_host_agent`, do that review and immediately
  run the provided `resume` command. If a command is live, wait on that exact
  command. If status is `failed`, diagnose the preserved error and resume the
  same operation.
- `terminal: true` with `checkpoint: calibration_service_ready`: the automated
  preparation is finished and the next actor is the user. This is valid only
  after the live service proves it has the selected registered domain, a usable
  vocabulary question, and the exact finalized terminology count.

Do not call the whole initialization complete at that handoff. Full
initialization completes only after the user submits 30 answers and the result
and personalized TSV are verified.

The mini corpus is built locally. When calibration starts, the Skill sends only
the compact word statistics listed in `vocabulary-calibration.md` plus the
user's isolated-word answers to the licensed predictor. PDFs, extracted text,
sentences, paper sources, and local paths never leave the confirmed workspace.

## Prepare the local runtime

Run commands from this Skill root. First check the Skill-local runtime.

macOS or Linux:

```bash
sh scripts/install.sh --check
```

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode check
```

If dependencies are missing, tell the user that the first installation may
temporarily use about 2 GB, settles near 1.2 GB after verification and cache
cleanup, and may take several minutes. Request network and disk-write permission
once, then run the platform installer yourself.

macOS or Linux:

```bash
sh scripts/install.sh --install
```

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode install
```

The launcher owns uv, managed Python 3.12, `.venv`, pinned packages, spaCy, and
the embedding model. Success requires exit code 0 and the final offline
verification reporting `"status": "ok"` after real spaCy and 384-dimensional
embedding inference. Do not create a second environment.

ResearchRamp uses OpenAlex and optionally arXiv; `arxiv-mcp-server` is not a
dependency. If `~/.researchramp/credentials.ini` has not been configured, use
the platform `configure_openalex` launcher. Never ask the user to paste an API
key into chat, never print it, and never store it in corpus artifacts. A saved
literal `anonymous` is a valid explicit choice. Do not use Semantic Scholar,
publisher crawling, or paywall bypasses.

## Confirm the research domain

Ask the user to describe the research problem in their own words. Then ask
exactly three or four concise follow-up questions that together clarify:

- the central phenomenon, object, or outcome;
- what is in scope and which adjacent interpretation is wrong;
- the methods, evidence, or output that matters;
- optionally, indispensable papers, authors, venues, or terms.

Summarize the domain in ordinary language and ask for confirmation. Only after
that confirmation, propose the retrieval scope and ask for confirmation of the
discipline/taxonomy boundary, providers, recent-year boundary, foundation-paper
allowance, and any seeds. Default to English public full text, the most recent
ten years, and at most ten older foundation papers. Do not expose page sizes,
retry policy, provider order, or parsing details.

Write the two confirmed decisions in the schema from `profile-format.md`.
Provider selection is fixed in this profile: OpenAlex is the backbone and must
receive the confirmed taxonomy filter; add arXiv only when its official category
taxonomy covers the discipline.

Propose one absolute local workspace and explain that it will contain PDFs,
text, caches, and analysis. Wait for explicit confirmation of that exact path.
Then create the directory and save the confirmed profile as
`research-profile-input.json`. The controller registers this exact workspace in
this Skill instance's own registry; never scan for domains or borrow another
Skill installation's registry.

## Run the one preparation operation

Tell the user:

> 你现在可以先离开这个任务，不需要守着对话；但在处理完成前，请暂时不要退出 Codex / Work Buddy 桌面应用，也不要关闭电脑。准备好后，我会直接打开校准页面让你回答 30 个单词。

Then run the controller. Use 70 papers for a real user build. The smaller value
10 is reserved for development acceptance experiments.

macOS or Linux:

```bash
.venv/bin/python scripts/initialize.py run \
  --profile <confirmed-workspace>/research-profile-input.json \
  --workspace <confirmed-workspace> \
  --target-papers 70
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\initialize.py run `
  --profile <confirmed-workspace>\research-profile-input.json `
  --workspace <confirmed-workspace> `
  --target-papers 70
```

Do not manually sequence `acquire_mini_corpus.py`,
`finalize_domain_assets.py`, `app/server.py`, or `open_workbench.py` during
normal initialization. The controller calls the deterministic helpers and
issues the host review requests. When a long helper yields a live session,
wait on the same session; do not launch a duplicate.

For each `host_action_required`, read `next_action.input` or every file in
`next_action.inputs`, write `next_action.output`, then immediately execute the
supplied `resume` command in this same task. The two possible host actions are:

1. Candidate review: select and order directly relevant title-and-abstract
   records, including enough relevant backups for failed links. Reject
   incidental keyword matches, off-scope disciplines, comments, replies,
   corrections, withdrawn records, and violations of the confirmed date or
   category boundary. Do not ask the user to screen papers.
2. Combined domain review: in one JSON, review every queued lemma and every
   terminology candidate. Correct only confirmed lemma/fused-form errors, drop
   only extraction noise, and keep stable shared multiword concepts supported
   by a representative source-paper sentence. Supply complete English meaning,
   Chinese meaning, concept role, and stable sense key for every selected term.

Every review JSON uses `schema_version: 1` and
`reviewer: current-host-agent`. After the combined review, the controller runs
`finalize_domain_assets.py` exactly once. That script finalizes and loads both
the vocabulary and terminology, writes one `domain-assets-summary.json`, and
returns success only when both are ready. Only then may the controller start
the calibration service.

The acquisition algorithm, corpus viability rule, evidence rules, and artifact
layout are specified once in `mini-corpus-workflow.md`. In particular, 60–69 of
a target 70 is usable; a 10-paper development run requires at least 9. A route
gets a bounded retry in the same invocation before fallback. Analysis runs only
after the usable minimum is reached, so an analysis artifact and a failing
corpus result cannot describe the same command.

## Hand the verified service to the user

Do not construct or launch the page yourself. The controller starts the domain
through the same registry-identified library service used for later daily
opening. It verifies the service identity, selected domain, calibration state,
embedded terminology, `/api/terms`, and the reviewed terminology count before
it can return:

```json
{
  "status": "awaiting_user_calibration",
  "terminal": true,
  "checkpoint": "calibration_service_ready"
}
```

Require `service.vocabulary_ready: true` and
`service.terminology_ready: true`. Open only `next_action.url`, tell the user
that the 30 questions use first reaction, and stop so the user can answer. The
terminology set is already final and does not depend on those answers.

When the user finishes, read and validate
`analysis/vocabulary-calibration-result.json` and
`analysis/personalized-vocabulary.tsv`. Report the factual corpus, vocabulary,
terminology, familiarity, and A/B/C/D counts, and link the result, personalized
TSV, finalized terminology TSV, and paper-decision audit. Only then say that
initialization is complete.

Later workbench openings must use `scripts/open_workbench.py`; it reuses the
same compatible live service when present. Weekly scheduling follows
`continuous-workflow.md` and is not forced into initialization.

## Invariants

- All documents and content stay in the user-confirmed workspace, and the Skill
  instance uses only its own explicit registry. The sole product-service data
  exception is the minimal word statistics and isolated-word answers required
  by the licensed predictor. Never scan the filesystem, infer another
  workspace, or copy domain data from another Skill installation.
- Vocabulary and terminology are both derived from retained full text. Titles
  and abstracts are for paper-level relevance only.
- Calibration never selects, removes, or redefines terminology.
- A helper checkpoint is never a task-completion signal. Only the controller's
  verified service handoff may return control to the user before calibration.
- `status.json` is a recovery and control contract, not a notification system.
