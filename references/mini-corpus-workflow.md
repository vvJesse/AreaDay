# Initial mini-corpus workflow

The initial run begins only after the user confirms both the interpreted research profile and the exact absolute local directory.

## Acquisition order

1. Read the research scope and retrieval scope that the user separately confirmed. These contain the accepted disciplines/categories, recent-year boundary, and older-foundation allowance.
2. Search OpenAlex across the confirmed query facets and send the confirmed `primary_topic.*` taxonomy filter with every request. The one-time local setup records either keyed or anonymous access in `~/.researchramp/credentials.ini`. A configured key increases the metadata budget and enables the cached OpenAlex content endpoint as a same-paper route. Never persist the key in corpus files. Reuse successful per-query metadata from this workspace when the same confirmed run is resumed.
3. Search arXiv only when the confirmed provider list includes it; every arXiv query must include subject categories and a confirmed date lane, with at least three seconds between requests. OpenAlex and arXiv may run as independent concurrent lanes, but arXiv requests within its own lane remain serial. Do not use Semantic Scholar.
4. Exclude obvious comments, replies, corrections, errata, and withdrawals. Interleave candidates across query groups and providers, cap the foundation lane, and deduplicate by DOI, OpenAlex ID, arXiv ID, or normalized title.
5. After metadata discovery, the current host agent reviews the bounded, coverage-preserving candidate packet, consults the retained complete list only when more candidates are needed, rejects incidental keyword matches and off-scope records, and writes an ordered list of up to 100 approved IDs. This is an internal controller checkpoint, not a stopping point and not a task for the user.
6. Download only reviewed public PDF routes returned by the providers. Try each route at most twice in the same invocation before moving to the next route. Try external OA locations and mirrors before the keyed OpenAlex content endpoint. Validate HTTPS, the PDF signature, and a 100 MB per-file ceiling. Use bounded concurrency across different papers, keep routes for one paper serial, and preserve reviewed result order. Continue to later reviewed candidates until the target is reached or the reviewed list is exhausted. The usable minimum is `ceil(target × 6/7)`: 60 for a target of 70 and 9 for a target of 10. Run corpus analysis only after that minimum is reached.
7. Keep every file and result in the confirmed local directory. Do not crawl publisher pages, bypass access controls, or substitute fuzzy title matches.

This order is automatic. The user does not need to choose a provider paper by paper.

## Local corpus analysis

1. Extract every valid downloaded PDF locally with PyMuPDF.
2. Keep the academic body while removing front matter before the abstract, references/bibliography, acknowledgements, repeated page headers and footers, page numbers, numeric-heavy rows, and duplicate captions. Preserve the cleaned text under `analysis/text/`.
3. Deduplicate versions using exact DOI, arXiv ID, or normalized title. Treat papers as near-duplicate versions only when title-token overlap is at least 0.85 and title-plus-abstract embedding similarity is at least 0.97. Keep the better-extracted version.
4. Compare title plus abstract with the confirmed research profile using the local embedding model. If at least eight usable papers are present, exclude only the extreme low-similarity tail from lexical statistics. Preserve every PDF and write the decision and score to the audit file.
5. Use spaCy over the retained papers' complete cleaned full text to build lemma frequency, document frequency, document share, Juilland dispersion, surface forms, representative sentences, and source-paper links.
6. Check the aggregated spaCy lemmas against the local spelling lexicon to create a high-recall suspicious-item queue. The current host agent reviews every candidate using its surface forms and full-text sentences, explicitly keeps valid technical vocabulary, corrects only confirmed lemma or fused-form errors, and drops only confirmed extraction noise. Every candidate must receive exactly one keep, replace, or drop decision before finalization. Apply this review to vocabulary records only; do not automatically rewrite extracted text or add PDF line-joining rules.
7. Mine multiword noun-phrase terminology candidates from that same full text, including counts, document spread, C-value, surface forms, possible acronyms, representative sentences, and sources.
8. Preserve that complete candidate set, then retain phrases appearing in at least 10% of the included papers as the shared-language review queue. Calculate the minimum with `ceil(included_papers × 0.10)`. Do not select a target output size or discard the raw candidate asset.
9. Complete the vocabulary spelling review first in `analysis/orthography-review-selection.json`, explicitly keeping, replacing, or dropping every suspicious lemma. Apply that review to produce the finalized canonical vocabulary. Only then check those finalized words against the bundled ECDICT glossary. Review every unresolved or context-sensitive vocabulary-card candidate together with the coverage-filtered terminology queue in `analysis/domain-review-selection.json`. The controller supplies bounded vocabulary-card batches sequentially; each input uses the top-level `.candidates` array. Verify its `candidate_count`, preserve prior reviewed entries across resumes, and never bulk-fill answers from the first dictionary entry. Keep only stable shared terminology of this research direction. Reject generic academic phrases, named entities, paper-local coinages, author-specific labels, redundant variants, and any candidate without a representative sentence tied to one of its source papers; do not create low-coverage exceptions. Supply a contextual English explanation, Chinese explanation, concept role, and stable sense key for every retained term. For each vocabulary-card candidate, supply a concise Chinese gloss keyed by its already finalized canonical lemma, exact cited evidence, and a candidate-specific rationale. A unique corpus acronym expansion controls the exact English meaning and suggested sense key and overrides a conflicting dictionary abbreviation. Run `scripts/finalize_domain_assets.py` once and require the semantic review contract plus exact candidate coverage and the vocabulary-card and terminology loaders to succeed before calibration can start.
10. Only after vocabulary-card and terminology finalization succeeds, continue into the local 30-question calibration page using this corpus's `analysis/vocabulary-map.tsv`. Store the answers and personalized result in the same `analysis/` directory. Corpus analysis alone is not the end of initialization. Keep the reviewed multiword terminology asset separate from the single-word calibration model; its selection does not depend on calibration answers. Vocabulary cards are prepared before this page opens; later briefs can add paper context but never replace their meanings.

Title and abstract are used for paper-level relevance only. Vocabulary and terminology evidence comes from full text. This first pass uses no general-English baseline, topic clustering, topic reweighting, or user-mastery inference, and it is not resized merely to reach a preferred entry count.

## Resumability

- Candidate metadata and per-query provider outcomes are saved locally.
- Successful OpenAlex and arXiv query results are reused for the same confirmed run; an explicit refresh bypasses them.
- Existing valid PDFs are reused.
- `download-results.jsonl` is rewritten after every attempt, so an interrupted run can continue from already valid PDFs.
- `run-timings.json` records internal timings; it is never a lifecycle-completion signal.
- `status.json` is the controller-owned lifecycle source. Any state with `terminal: false` requires the current host task to continue.

The process remains in the current desktop task. There is no custom notification subsystem; completion is the task's ordinary final response.

## Output layout

```text
<confirmed-directory>/
  research-profile-input.json   Chat-confirmed input profile
  research-profile.json         Profile copied into the run record
  status.json                   Authoritative preparation state and next actor
  candidates.jsonl             New multi-provider candidates for this run
  candidate-review-packet.jsonl Coverage-preserving bounded host-review tranche
  candidate-review-summary.json Review-packet and deferred-candidate counts
  search-attempts.json          Per-query provider outcomes
  run-timings.json              Cross-command phase and wall-clock timings
  candidate-review-selection.json Host-agent reviewed and ordered IDs
  papers/                       Locally stored PDFs
  download-results.jsonl        Per-paper provider attempts and result
  cold-start-summary.json       Acquisition and analysis totals
  analysis/text/                Locally extracted text
  analysis/papers.jsonl         Per-PDF extraction and analysis result
  analysis/paper-decisions.jsonl Auditable include/duplicate/relevance decisions
  analysis/vocabulary-map.tsv   Full-text lemma candidates and corpus statistics
  analysis/vocabulary-map.jsonl Same candidates with structured contexts
  analysis/vocabulary-card-catalog.jsonl Stable bilingual learning cards prepared before calibration
  analysis/orthography-review-input.json High-recall contextual spelling review queue
  analysis/orthography-review-selection.json Explicit keep, replace, or drop decision for every suspicious lemma
  analysis/orthography-review-summary.json Applied correction counts and provenance
  analysis/raw-terminology-candidates.tsv Complete full-text multiword candidates
  analysis/raw-terminology-candidates.jsonl Same complete candidates in structured form
  analysis/terminology-candidates.tsv Candidates found in at least 10% of included papers
  analysis/terminology-candidates.jsonl Same coverage-filtered candidates in structured form
  analysis/terminology-review-input.json Host-review context and schema
  analysis/terminology-review-selection.json Host-selected terms and exact explanations
  analysis/first-terminology-map.jsonl Host-reviewed domain terminology
  analysis/first-terminology-map.tsv Same finalized terminology for export
  analysis/terminology-explanations.json Exact bilingual explanations for the reviewed terms
  analysis/host-review-summary.json Terminology finalization counts and provenance
  analysis/vocabulary-calibration-session.json The user's 30 direct answers
  analysis/vocabulary-calibration-result.json Personalized counts, selected 75%–98% threshold, protected boundary count, and importance tiers
  analysis/personalized-vocabulary.tsv Per-word familiarity prediction, classification, importance tier, protection flag, and selected threshold
  analysis/corpus-stats.json    Corpus extraction and lexical statistics
  analysis/summary.md           Human-readable raw-analysis summary
```
