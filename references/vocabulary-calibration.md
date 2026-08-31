# Vocabulary calibration model

This local step follows mini-corpus analysis. It reads that corpus's Vocabulary
Map, asks 30 isolated-word familiarity questions, and keeps its state and final
personalized predictions inside the same corpus directory.

Responses are stored in
`<confirmed-corpus-directory>/analysis/vocabulary-calibration-session.json`.
After answer 30, the server automatically writes
`analysis/vocabulary-calibration-result.json` and
`analysis/personalized-vocabulary.tsv`; the page also offers the TSV download.

The model starts with the original `wordfreq` population prior and adds weak
education evidence for the default Chinese B2/CET-6 learner profile:

- CEFR-J: `+0.80` for A1, `+0.60` for A2, `+0.35` for B1, `+0.15` for B2.
- ECDICT exam tags: `+0.30` for Gaokao, `+0.20` for CET-4, `+0.10` for CET-6.
- No CEFR-J or applicable exam evidence: no adjustment.

Because these lists overlap, the model takes the strongest applicable
education adjustment rather than adding correlated signals. It then estimates
one person-specific ability shift with the same small Bayesian model and only
excludes words whose posterior probability of being known reaches the selected
threshold. The result page starts at the recommended 90% setting and internally
provides whole-number steps over 75%–98%. The UI deliberately presents this as
an outcome-oriented `collect fewer → recommended starting point → collect more`
continuum, rather than asking users to interpret the engineering probability
bounds. It shows the live personal-list count and places the control immediately
above two samples labelled `not added to the list` and `added to the personal
list`. Per-word probabilities remain in the machine-readable result but are not
shown in the UI. The guidance recommends a boundary where most words are known,
some take a few seconds to recall, and a small number are genuinely unknown.
Moving left produces a shorter list; moving right keeps more boundary words.
This changes only the final classification, not the fitted probabilities or the
30-question model.

To avoid losing high-value domain vocabulary at an uncertain boundary, A/B-tier
words (found in at least five corpus papers) are retained when their predicted
known probability is no more than five percentage points above the selected
threshold. The page reports this protected count separately, and the TSV marks
these rows as `important_boundary`.

Direct answers always override model output for that exact word: `known` is
exported as `1.0`, `unknown` as `0.0`, and `unsure` remains model-derived.

Use both `--disable-cefr-prior --disable-exam-prior` to reproduce the original
word-frequency-only baseline. `--exam-profile` accepts `none`, `gaokao`,
`cet4`, or `cet6`. This is a lightweight personalization model, not a validated
language-assessment instrument.

An unlisted word is deliberately not penalized: the first comparison showed
that a negative adjustment disproportionately pushed valid domain terms such
as `reproducibility`, `disentangle`, and `explainable` down merely because a
general-learning list did not contain them.
