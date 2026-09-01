# Research brief generation and scheduling

Read this reference when the user asks to generate a research brief now, set or
change weekly brief generation, or set or change a daily review reminder.
Initialization remains governed by `full-workflow.md`. Viewing existing briefs
does not require this reference and never starts generation.

## Product routing

Before immediate generation or weekly scheduling, apply the shared domain
resolution rule in `SKILL.md`. Domain resolution must finish before discovery,
settings writes, or task creation. A weekly task is permanently handed the
resolved domain ID and workspace, so its later runs never infer or reselect a
domain.

- **查看简报** opens the workbench's briefs view. It does not generate data or
  create a scheduled task.
- **生成简报** runs one complete generation operation and saves one new brief.
- **设置每周简报** creates or updates one weekly task that runs that same
  generation operation.
- **设置每日复习提醒** creates or updates one independent due-review task.

The deterministic scripts also expose internal discovery, preparation,
finalization, and due-count operations. Do not present these as separate product
commands.

## One brief-generation operation

Run the operation only through `scripts/generate_brief.py`. The controller owns
the operation until it returns a terminal result. Whenever it requests a
current-host-agent action, write the requested output and immediately run its
exact resume command. Do not stop at any internal checkpoint.

1. Run `continuous_workflow.py discover` for the initialized workspace. This
   retrieves metadata only, records provider outcomes, and updates the durable
   unrecommended candidate pool under `continuous/discovery/`. Discovery reuses
   the providers and scholarly-discipline boundaries confirmed during init:
   OpenAlex searches include the stored `primary_topic.*` filter, and arXiv is
   queried only when the profile contains confirmed categories. Semantic
   Scholar is not used. The ordinary pass searches a rolling four-year window
   and labels papers published within the
   latest 14 days as `new_paper`; the remainder are `recent_paper`. Candidates
   not selected this week remain available for later briefs and are reclassified
   as they age.
2. Review those titles and abstracts as the current host agent. Research
   relevance and value are the first decision; recency breaks ties between
   similarly useful papers. The controller supplies the new, recent, and
   confirmed classic lanes together so this review happens once. Prefer strong
   new and recent work; use older classic papers only when those lanes remain
   insufficient. Classic papers keep the `classic_paper` label.
3. If the new, recent, and classic lanes together still contain fewer than two
   strong items, use the host's normal web research capability to find directly
   readable public reports or research updates. Use only a real public page or
   PDF whose title, date, publisher, and body can be verified. Do not invent a
   source, use inaccessible content, or add a weak item merely to reach a count.
4. Select two to five items in total. Save a selection file with this shape:

```json
{
  "schema_version": 1,
  "period_start": "2026-08-24",
  "period_end": "2026-08-30",
  "selected_candidate_ids": ["OpenAlex:W123", "OpenAlex:W456"],
  "supplemental_items": [
    {
      "item_type": "public_report",
      "title": "Verified public report title",
      "source_url": "https://example.org/report",
      "content_url": "https://example.org/report.pdf",
      "publication_date": "2026-08-27",
      "venue": "Publishing organization",
      "abstract": "Short factual description used during review."
    }
  ]
}
```

`selected_candidate_ids` contains papers already present in the candidate pool.
`supplemental_items` is optional and is used only after all paper lanes remain
insufficient. `content_url` is optional when `source_url` itself exposes the
readable page or PDF. Item types are assigned by the deterministic workflow;
the selection file must not relabel paper freshness.

5. Run `continuous_workflow.py prepare`. It temporarily downloads only the
   selected public PDFs or readable pages, verifies that the fetched title and
   text match the selected source, extracts full text locally, and compares that
   text with the user's personalized vocabulary map. Read the produced
   `agent-brief-input.json` and its temporary text paths.
6. As Codex / Work Buddy, write all research-value explanations, the brief
   summary, contextual bilingual word meanings, and shadow previews. Do not call another
   model API. Produce exactly the prepared sources. Preserve truthful types:
   `new_paper`, `recent_paper`, `classic_paper`, `public_report`, or
   `research_update`.
7. Save the result at the packet's `agent_output_path` using the schema below,
   then run `continuous_workflow.py finalize`. Preserve every prepared source's
   item ID, title, canonical source URL, type, vocabulary lemma, and extracted
   context exactly. Finalization rejects crossed titles/links and invented paper
   contexts, then imports the brief before deleting that run's temporary PDFs
   and full text.

## Agent brief output

```json
{
  "brief_id": "brief-20260901T103000Z-a1b2c3d4",
  "period_start": "2026-08-24",
  "period_end": "2026-08-30",
  "headline": "A plain-language headline for this brief",
  "summary": "A short overall report that helps the user choose.",
  "items": [
    {
      "item_id": "OpenAlex:W123",
      "item_type": "new_paper",
      "title": "Paper title",
      "source_url": "https://example.org/paper",
      "publication_date": "2026-08-28",
      "venue": "Venue",
      "value_reason": "Why this is useful for the confirmed research question.",
      "estimated_minutes": 24,
      "shadow_preview": "One or more short original preview paragraphs.",
      "vocabulary": [
        {
          "lemma": "implicature",
          "meaning_en": "meaning suggested in conversation without being explicitly stated",
          "meaning_zh": "会话中未直接说出但可推导的含义",
          "part_of_speech": "noun",
          "context": "A short representative source context.",
          "sense_key": "conversationally-implied-meaning",
          "confidence": 0.98,
          "evidence_context_id": "stable ID for this extracted source context"
        }
      ]
    }
  ]
}
```

Do not copy long source passages. An item appearing in a brief does not change
the vocabulary state. When the user selects it and presses `开始预热`, every
predicted unfamiliar word receives one global learning record. Existing records
with the same lemma and sense key only gain a new source. A materially different
sense receives its own learning item. Words marked mastered remain mastered.

The review page keeps due FSRS work separate from new-term confirmation. Its
navigation badge counts only due learning items. The optional term flow presents
five unconfirmed, host-reviewed terms at a time, prioritizing terms connected to
the latest brief and then broader corpus coverage. `需要学习` creates a global
FSRS item, `已经会了` records global mastery, and `这次跳过` leaves the term
unconfirmed. The full domain terminology asset remains available through a
20-item paginated library with fuzzy search and status filters; it is not itself
an automatic review queue.

## Scheduled tasks

Resolve the domain and obtain the schedule before a task is created. Use
`scripts/configure_schedule.py` to save only the requested preference and emit
exactly one automation handoff:

```bash
.venv/bin/python scripts/configure_schedule.py weekly --weekday <1-7> --time <HH:MM> [--domain <domain-id>]
.venv/bin/python scripts/configure_schedule.py daily --time <HH:MM> [--domain <domain-id>]
```

Create or update the single host task in that handoff. The weekly task runs the
complete brief-generation operation above. The daily task runs `due-count` and
notifies only when one or more words or terms are due. The two preferences and
tasks are independent. Do not create a separate background-discovery task, do
not change the other schedule, and do not create duplicate tasks with the same
`automation_key`.

The workbench schedule page follows the same separation: each form saves one
preference and emits one handoff. Saving a preference is not itself proof that
the host task was successfully registered.

## Retention and cleanup invariants

- Brief history, derived previews, and metadata remain inside each domain's
  `continuous/` directory. Global word/term state, FSRS cards, and review logs
  live beside the explicit domain registry so multiple registered domains share
  one learner model without merging their corpora.
- Temporary PDF and full text live only in `continuous/working/<run-id>/` and
  are removed only after successful import and archival of the finished brief.
- Never delete a failed run automatically; preserve it for diagnosis or retry.
- Never label an older paper as a new paper.
