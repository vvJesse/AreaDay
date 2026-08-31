---
name: researchramp-full-workflow
description: "Build or continue a local ResearchRamp for a confirmed research area: establish a personal domain vocabulary, schedule automatic weekly research briefs and review reminders, view current or past briefs, and review due vocabulary or terminology. Use for `$researchramp init`, `$researchramp schedule`, and requests to open or continue an existing ResearchRamp."
---

# ResearchRamp full workflow

This reference contains the slow, uncommon workflows for first-time domain
initialization and scheduled weekly research. The public product model and
ordinary workbench-opening behavior live in `SKILL.md` and take precedence.

Read this file only for initialization or for the advanced weekly pipeline. Do
not read or apply it when the user merely wants to open the workbench, see a
brief, view vocabulary, review learning items, or switch domains.

Run initialization inside the current desktop chat. Store the research profile,
paper files, extracted text, vocabulary data, briefs, and review history only in
the user-confirmed local directory.

## 1. Install and verify the local runtime

Run every command in this Skill's root directory. Do not require the user to have Python or uv, and do not run `python scripts/setup_dependencies.py` with an unverified system interpreter.

The first installation immediately opens two local files before downloading the heavy runtime: a static OpenAlex help page in the browser and `~/.researchramp/credentials.ini` for editing. The configuration file may appear in the Codex or WorkBuddy right-hand file panel; depending on the host, it may instead open in the operating system's text editor. The help page only explains how to obtain a key; it never receives the key. The configuration file contains one clearly commented `api_key =` field. Dependency installation continues while the user edits that file, then the launcher waits for the saved value to be validated. This flow does not start a localhost server and does not wait for Python, uv, spaCy, Sentence Transformers, or model downloads. ResearchRamp does not read `OPENALEX_API_KEY`, `OPENALEX_EMAIL`, or `OPENALEX_MAILTO` and never modifies shell profiles or environment variables.

First perform a read-only runtime check using the launcher for the current operating system.

macOS or Linux:

```bash
sh scripts/install.sh --check
```

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode check
```

If the check succeeds but `~/.researchramp/credentials.ini` is absent or incomplete, run the matching one-time setup launcher below before continuing to the research-area conversation. If the runtime check fails, tell the user that the first installation may temporarily use up to about 2 GB while downloads are cached, settles at roughly 1.2 GB after successful verification and cache cleanup, and may take several minutes. Request permission for the network and disk writes before installing; the installation launcher opens the static help page and editable configuration file immediately while dependency downloads continue in the background.

macOS or Linux:

```bash
sh scripts/configure_openalex.sh
```

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\configure_openalex.ps1
```

The setup launcher creates the configuration file with user-only permissions, opens it for editing, and watches only that exact file for a saved value. Tell the user to look for `credentials.ini` in the Codex or WorkBuddy right-hand file panel; depending on the host, it may instead appear in the operating system's text editor. It validates a key directly with OpenAlex over HTTPS without printing it or placing it in process arguments. Entering the literal value `anonymous` is the explicit anonymous-access choice. The launcher exits only after one choice succeeds. Do not ask the user to paste a key into chat, do not read or display the completed file yourself, and do not tell the user they may leave until this setup has finished. No application restart is needed.

After approval, run exactly one platform launcher.

macOS or Linux:

```bash
sh scripts/install.sh --install
```

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode install
```

The launchers install a pinned uv into `.runtime/uv` without changing the user's shell profile, ask uv for managed Python 3.12, keep uv's Python and download cache under `.runtime/`, create the Skill-local `.venv`, install the pinned scholarly HTTP, PDF, spaCy, and Sentence Transformers packages, install `en_core_web_sm`, and run real inference. The public `sentence-transformers/all-MiniLM-L6-v2` model is stored under `~/.researchramp/models/` and reused across Skill updates. A Hugging Face token is not required.

Installation is successful only when the launcher exits with code 0 and its final offline verification reports `"status": "ok"` after loading spaCy and producing a 384-dimensional embedding. A package-install success message alone is insufficient. After success, the launcher removes only uv's disposable package-download cache; the verified environment, managed Python, uv binary, and NLP models remain. If installation fails, preserve its caches, explain the failed source or stage, and do not begin corpus construction.

Pinned Python packages are tried from PyPI and then established mainland-China mirrors, so users on constrained networks do not need to configure an index during normal installation. A release or managed deployment may override the first source with `RESEARCHRAMP_PYPI_INDEX_URL`. The embedding download uses the asset endpoint pinned in `embedding-model-manifest.json` when one is configured for a release, then Hugging Face, then `hf-mirror.com`. Every model source must produce the same pinned revision and pass all SHA-256 checks. Do not ask the user to choose or configure a source during normal installation.

When preparing a paid release and its static asset bucket, read [model-hosting.md](model-hosting.md). Normal users and normal initialization runs do not need to read it.

The uv bootstrap tries the official GitHub release before Astral's release proxy and verifies the release checksum. A packaged release may set `RESEARCHRAMP_UV_INSTALLER_URL` and `RESEARCHRAMP_UV_DOWNLOAD_URL` to its own pinned COS/OSS mirror without changing this workflow.

If a network step fails, report which access is likely missing: the pinned uv installer, every configured uv binary source, uv's managed Python source, the configured Python package index, or every configured static model source. Rerun the same platform launcher after network/proxy correction; verified cached files and the existing environment are reused. Do not create an unrelated second environment.

Do not substitute publisher crawlers, browser scraping, or paywall-bypass tools. Do not ask the user to paste an API key into chat. Use OpenAlex as the cross-disciplinary discovery backbone. It works with the explicit keyed or anonymous choice stored in the one-time local configuration file; keyed access increases the request budget and provides an additional same-paper PDF route when the selected record exposes cached content. Never save the key in corpus artifacts. Do not use Semantic Scholar. Add arXiv only when the confirmed discipline has meaningful arXiv category coverage; otherwise use OpenAlex alone.

## 2. Understand the research area in chat

First ask the user to describe the research area or problem in their own words.

Then ask exactly 3 or 4 concise follow-up questions. Adapt them to what the user already said; do not ask them to repeat answered information. Together, the questions must clarify:

- the central phenomenon, object, or outcome;
- what is in scope and which adjacent interpretation would be wrong;
- the methods, evidence, or research output the user cares about;
- optionally, seed papers, authors, venues, or indispensable terms.

After the answers, summarize the understood research area in ordinary language and explicitly ask the user to confirm or correct it. Do not search for papers before this confirmation.

After the research summary is confirmed, prepare one compact retrieval-scope proposal and ask the user to confirm or correct it. Include only decisions that materially change the corpus:

- the primary scholarly discipline, the exact OpenAlex topic/subfield/field/domain boundary, the selected providers, and one adjacent interpretation being excluded;
- the recent-year boundary;
- whether older foundational papers are allowed and their maximum count;
- any user-specified seed papers, authors, venues, or preferred study types.

Default to English public full text, the most recent ten years, and no more than 10 older foundational papers. These are proposals, not hidden rules. Do not ask the user about API page sizes, provider order, retry policy, deduplication, or local parsing details.

Convert both confirmations into the profile format in [profile-format.md](profile-format.md). Set the research profile and retrieval scope to confirmed only after the user approves them. The Skill ships no default research profile; every profile belongs to the user-confirmed domain workspace.

Provider selection is completed here and then frozen into the confirmed profile. OpenAlex always receives the stored `primary_topic.*` taxonomy filter. Add arXiv only when its official category taxonomy covers the confirmed discipline, and store the selected categories in the same profile. Weekly discovery must reuse this exact route rather than deciding the discipline or providers again.

## 3. Confirm the local paper directory

Propose one clear absolute local directory based on the confirmed topic. Tell the user that PDFs, extracted text, caches, and analysis will be stored there. Wait for explicit confirmation of that exact path. Do not create the directory or download anything before confirmation.

After confirmation, save the profile JSON inside that directory as
`research-profile-input.json`, then immediately register the domain in this
Skill instance's registry. At this point the registry already has everything it
needs: the stable profile ID, user-facing label, and confirmed absolute
workspace path. It does not contain papers, vocabulary, calibration answers, or
completion results.

macOS or Linux:

```bash
.venv/bin/python scripts/domain_registry.py register \
  --workspace <confirmed-directory> \
  --domain-id <profile-id> \
  --label <short-research-area-label>
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\domain_registry.py register `
  --workspace <confirmed-directory> `
  --domain-id <profile-id> `
  --label <short-research-area-label>
```

The host agent runs this command; do not ask the user to run it. Registration
must succeed before any paper search begins. The first corpus command repeats
the same registration idempotently before discovery so it cannot produce a
corpus or vocabulary for an unregistered workspace.

## 4. Start the initial corpus run

Before starting, tell the user in plain language:

> 你现在可以先离开这个任务，不需要守着对话；但在处理完成前，请暂时不要退出 Codex / Work Buddy 桌面应用，也不要关闭电脑。处理结束后，这个任务会像普通任务一样给出最终回复。

This is only a conversational expectation. Do not implement a custom notification, scheduled reminder, detached service, or polling message.

Run discovery first with the verified Skill-local interpreter. This stage searches metadata only and does not download PDFs.

macOS or Linux:

```bash
.venv/bin/python scripts/acquire_mini_corpus.py \
  --profile <confirmed-directory>/research-profile-input.json \
  --workspace <confirmed-directory> \
  --target-papers 70 \
  --openalex-per-query 50 \
  --arxiv-per-query 60 \
  --search-only
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\acquire_mini_corpus.py `
  --profile <confirmed-directory>\research-profile-input.json `
  --workspace <confirmed-directory> `
  --target-papers 70 `
  --openalex-per-query 50 `
  --arxiv-per-query 60 `
  --search-only
```

If a long command yields a running session, continue waiting on that same session. Do not start a duplicate run.

After discovery, review `candidates.jsonl` yourself in title-and-abstract batches. This is a required host-agent decision, not a task for the user and not a separate model API call. Reject papers that:

- use a search phrase only incidentally;
- address a different phenomenon or discipline than the confirmed scope;
- are comments, replies, corrections, withdrawn papers, or other non-research records;
- violate the confirmed date/category boundary;
- lack enough title-and-abstract evidence to justify downloading.

Retain directly relevant papers and legitimate methodological/background papers. Older papers may be selected only from the explicit foundation lane and may not exceed `foundation_limit`. Preserve the confirmed research angles without forcing equal quotas. Select up to 100 ordered candidate IDs so failed PDF links have backups; the first 70 should be the strongest set, not merely the first 70 returned by a provider. Save:

```json
{
  "schema_version": 1,
  "reviewer": "current-host-agent",
  "selected_candidate_ids": ["arXiv:2407.18367"],
  "review_summary": "Short factual explanation of scope and rejections"
}
```

If fewer than 70 plausible candidates remain, revise phrases inside the already confirmed scope and rerun discovery. A completed run with 60–69 valid PDFs is still a usable initial corpus; report the shortfall instead of padding it with off-scope papers. Ask the user again only if the needed revision changes a confirmed discipline, date boundary, or research interpretation.

Then download the reviewed set and run local full-text analysis:

macOS or Linux:

```bash
.venv/bin/python scripts/acquire_mini_corpus.py \
  --profile <confirmed-directory>/research-profile-input.json \
  --workspace <confirmed-directory> \
  --target-papers 70 \
  --selection <confirmed-directory>/agent-candidate-selection.json \
  --analyze
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\acquire_mini_corpus.py `
  --profile <confirmed-directory>\research-profile-input.json `
  --workspace <confirmed-directory> `
  --target-papers 70 `
  --selection <confirmed-directory>\agent-candidate-selection.json `
  --analyze
```

The pipeline performs two distinct kinds of analysis:

- It uses each paper's title and abstract only for conservative paper-level relevance checking.
- It extracts, cleans, tokenizes, lemmatizes, and mines vocabulary and terminology from the retained papers' full text.
- It preserves the complete noun-phrase terminology candidate set, then keeps only phrases found in at least 10% of the retained papers as the shared-language review queue. For 98 papers, that means at least 10 papers. This is an eligibility gate, not proof that a phrase is a term.

After the command exits successfully, first inspect `analysis/orthography-review-input.json`. It is a high-recall queue of spaCy lemmas absent from the local spelling lexicon, not a list of confirmed errors. Review each item's surface forms and corpus sentences yourself. Leave valid technical terms, names, abbreviations, and legitimate variants unchanged by omitting them. Save only confirmed corrections and extraction-noise drops as:

```json
{
  "schema_version": 1,
  "reviewer": "current-host-agent",
  "lemma_replacements": {"pretraine": "pretrain"},
  "lemma_drops": ["confirmed extraction fragment"],
  "review_summary": "Short factual explanation"
}
```

Then apply that review to the aggregated vocabulary records:

macOS or Linux:

```bash
.venv/bin/python scripts/apply_orthography_review.py \
  --workspace <confirmed-directory> \
  --selection <confirmed-directory>/analysis/orthography-review-selection.json
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts\apply_orthography_review.py `
  --workspace <confirmed-directory> `
  --selection <confirmed-directory>\analysis\orthography-review-selection.json
```

The spelling checker must never rewrite text or lemmas automatically. This correction happens only in the aggregated vocabulary records; do not add PDF line-joining rules or rerun PDF extraction for it.

Do not end the task after corpus analysis or spelling review. The next required user-facing stage is personal vocabulary calibration. Start the local calibration service against this exact corpus:

macOS or Linux:

```bash
.venv/bin/python app/server.py \
  --corpus <confirmed-directory> \
  --exam-profile cet6 \
  --host 127.0.0.1 \
  --port 8765 \
  --no-browser
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe app\server.py `
  --corpus <confirmed-directory> `
  --exam-profile cet6 `
  --host 127.0.0.1 `
  --port 8765 `
  --no-browser
```

If port 8765 is occupied by an earlier ResearchRamp calibration service, stop that exact old process before starting the new corpus service. Do not use an old session, an application-global state file, or another corpus's vocabulary. Open `http://127.0.0.1:8765/` for the user and verify that `/api/state` reports the current profile label, zero answers, and the current Vocabulary Map's entry count.

Tell the user that this is the next step of the same initialization. Ask them to answer all 30 words by first reaction. Do not report the ResearchRamp initialization as complete while the page still has unanswered questions. Read [vocabulary-calibration.md](vocabulary-calibration.md) when the model assumptions or bundled prior data need inspection.

After answer 30, the service automatically writes:

- `analysis/vocabulary-calibration-session.json`: the 30 direct answers;
- `analysis/vocabulary-calibration-result.json`: posterior counts, boundaries, importance tiers, and provenance;
- `analysis/personalized-vocabulary.tsv`: every corpus word with its predicted familiarity and retained/excluded classification.

These files remain entirely inside the already registered workspace. Answering,
resetting, or adjusting calibration must never write, remove, or alter the
domain's registry entry.

The result page presents corpus importance before the range control, then places that control immediately above the two boundary-word samples so the user can compare while adjusting. Use one consistent user-facing model: words either `暂不加入词表` or `将加入个人词表`; avoid mixing this with the engineering terms excluded/retained. It defaults to the recommended 90% exclusion threshold and internally supports 75%–98% in one-percentage-point steps. Do not expose those bounds or per-word probabilities in the UI. Show an outcome-oriented continuum from `少收一些` through `推荐起点` to `多收一些`, lead with the live personal-list count, and keep a `恢复推荐起点` action. Explain the recommended stopping point: most boundary words should be recognizable, some should take a few seconds to recall, and a small number may be genuinely unknown. If nearly all are recognized instantly, move left; if many are completely unknown, move right. The adjustment changes only the final classification, never the learned probability or question history. Preserve the importance safeguard: an A/B-tier word whose known-probability is within five percentage points above the selected threshold remains in the list as `important_boundary`. Do not merge probability and corpus importance into a single score.

Read the completed result file yourself. Only then report the personalized counts and A/B/C/D importance breakdown. The raw terminology candidates and their document-coverage-filtered review set remain separate corpus-derived assets; do not silently mix multiword terminology into the single-word familiarity model. Review the filtered candidates once as the current host agent. Keep only stable shared terminology of the confirmed research direction; reject generic academic phrases, named entities, paper-local coinages, author-specific labels, and redundant variants. Do not create exceptions for low-coverage article-local concepts. For each retained term, write a contextual `meaning_en`, `meaning_zh`, `concept_role`, and stable `sense_key`, but no separate selection reason. Save the exact selected-term explanations to `analysis/terminology-explanations.json`; do not generate generic definitions at page-render time.

After verifying the completed result and export, verify that the calibration
response reports success. Do not perform another registration step here: the
domain has already been registered since its workspace was confirmed.

## 5. Report the result

When the 30-question calibration finishes, give the normal final task reply. State the confirmed directory and the factual counts for candidates found, PDFs downloaded, failed download attempts, PDFs analyzed, papers included, duplicates excluded, extreme low-relevance papers excluded, raw Vocabulary Map entries, terminology candidates, predicted familiar words, uncertain words, likely unfamiliar words, and A/B/C/D retained-word counts. Link the user to `analysis/personalized-vocabulary.tsv`, `analysis/vocabulary-calibration-result.json`, the terminology candidate TSV, and the paper-decision audit.

Also tell the user that initialization is complete and that they can later run `$researchramp schedule` to **开启或修改每周研究简报** and its due-review reminder. Do not force schedule configuration into init mode.

## 6. Open the unified local application

After calibration is complete and registered, use the same local application
for all user-facing views. The library reads only explicitly registered
workspaces and presents a research-area selector when two or more exist. Run
from this Skill directory:

macOS or Linux:

```bash
.venv/bin/python app/server.py \
  --library researchramp-data/real-domains.json \
  --domain <profile-id> \
  --mode <vocabulary|briefs|review|schedule> \
  --host 127.0.0.1 \
  --port 8765 \
  --no-browser
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe app\server.py `
  --library researchramp-data\real-domains.json `
  --domain <profile-id> `
  --mode <vocabulary|briefs|review|schedule> `
  --host 127.0.0.1 `
  --port 8765 `
  --no-browser
```

For schedule mode, add `--exit-on-settings-save`, open the local URL, and wait on that exact process. The page saves preferences and exits; read the emitted `automation_handoff`, then use the host platform's supported scheduling capability to create or update the named recurring tasks. Never claim scheduling succeeded merely because the local JSON file was saved.

In library mode, the user-facing URL must always identify both the domain and the view. Open and give the user the exact URL `http://127.0.0.1:8765/?domain=<profile-id>#<mode>`, substituting the registered domain ID and requested view. Never give a root-only or hash-only URL such as `http://127.0.0.1:8765/#briefs` when more than one domain is registered: without `?domain=<profile-id>`, the page can appear to contain no data and the user may reasonably think ResearchRamp is broken. Verify the exact user-facing URL itself after opening it; seeing a correct page in an already-open side panel is not sufficient proof that the link sent to the user works.

Every browser request after the initial application load is scoped to the
selected domain. Switching domains changes the corpus vocabulary, terminology
importance, briefs, and schedule settings. The learner's word/term mastery and
FSRS queue are global across registered domains; the same sense reuses one card,
while a materially different sense receives a separate card. Never combine
domain corpus rows or briefs, and never implement switching by changing a
process-global current workspace.
The review view has two separate entrances: due FSRS review and an optional
five-term confirmation batch. The navigation count reports only genuinely due
learning items. A new term enters FSRS only after the user selects that it needs
learning; a term marked understood becomes globally mastered, and a skipped term
remains unconfirmed. Keep the full terminology library as a searchable,
status-filtered, paginated reference view rather than rendering every term as one
unbroken page.
Any vocabulary-mastery percentage shown for a domain must use only words that
entered that domain's personal vocabulary as its denominator. Split A/B words
from C/D words. Count both important-boundary words that calibration judged
known but retained because of the A/B importance safeguard, and words the
learner later explicitly marked mastered in reading or review. Never use the
raw Vocabulary Map or words excluded from the personal vocabulary as the
denominator or numerator of this progress percentage.
For diagnosis or a one-domain initialization page, `--corpus` remains valid;
ordinary opening after initialization uses `--library`.

### Weekly brief semantics and multi-domain schedules

**每周研究简报** means an automatically generated scheduled artifact. Domain
selection happens while the schedule is configured, not when the weekly job
runs. If only one domain is initialized, configure that domain. If two or more
domains are initialized and the user asks to enable weekly briefs without naming
the intended domains, ask one concise clarification listing the registered
user-facing domain labels and an `all domains` option. An ambient or already-open
domain may be recommended first but is not itself authorization.

Create or update a separate domain-scoped schedule for every selected domain.
The user may choose the same time for several domains, but every scheduled run
already carries its own domain ID and workspace and must never ask again which
domain to use. Each run creates one independent brief. Never merge candidate
pools, source text, recommendation history, vocabulary, terminology, or brief
history across domains. Report failures per domain without invalidating briefs
that succeeded for other domains.

Use **开启或修改每周研究简报** for schedule configuration, **查看本周简报**
for the latest existing artifact, and **查看或搜索往期简报** for history. Do not
describe a manual prompt as the normal way to generate a weekly brief. A one-off
`立即更新` or `临时调研` is not a public ResearchRamp capability unless that
feature is explicitly added later.

For weekly discovery, host-agent briefing, shadow-preview generation, FSRS reminders, and temporary full-text cleanup, read [continuous-workflow.md](continuous-workflow.md). Weekly discovery is freshness-tiered: prefer strong new papers, then recent unrecommended papers, then older high-value papers; only when those lanes remain insufficient may the host add a verified public report or research update with readable source text.

Read [mini-corpus-workflow.md](mini-corpus-workflow.md) for acquisition order, resumability, and output files.

## Fixed boundaries for this stage

- Start discovery from the confirmed profile in the current run. Do not read an earlier candidate list or count an earlier corpus toward the target.
- Search OpenAlex with the confirmed English and year boundaries using the explicit local keyed or anonymous setting. Try its recorded external OA locations before spending a keyed OpenAlex content request.
- For arXiv-enabled profiles, search phrases only in titles and abstracts, require one of the confirmed categories, apply the confirmed date lane, and keep at least three seconds between API requests. Never use an unqualified `all:` search.
- Exclude comment/reply/correction/withdrawal records before agent review. Interleave the surviving provider results, deduplicate by DOI, OpenAlex ID, arXiv ID, or normalized title, and preserve the query provenance and scholarly-discipline constraint.
- Never download directly from raw search order. The host agent must first approve and order candidate IDs from title and abstract; downloading stops after 70 valid PDFs or exhaustion of that reviewed list.
- Never use fuzzy title matching to substitute a different paper.
- Remove only duplicate versions and the extreme low-relevance tail before lexical statistics. Exact DOI, arXiv ID, or normalized title matches are duplicates; near duplicates require both near-identical titles and very high title/abstract embedding similarity. Keep all PDFs and record every analysis decision.
- Use title and abstract for paper-level relevance only. Build vocabulary and terminology candidates from cleaned full text, not merely metadata.
- Do not add a general-English baseline, topic clustering, topic reweighting, or user-mastery inference in this stage.
- The raw Vocabulary Map and Terminology Map are corpus-derived domain assets, not claims about personal knowledge. Only the completed 30-question calibration produces the first personalized vocabulary prediction.
- `status.json` exists only for recovery and diagnosis. It is not a notification mechanism.
