# Bundled vocabulary-prior sources

`cefr_j_v1_6.tsv.gz` is a normalized two-column extract of **The CEFR-J
Wordlist Version 1.6**, compiled by Yukio Tono, Tokyo University of Foreign
Studies. The source workbook explicitly permits research and commercial use
with proper acknowledgement.

Official source:
<http://www.cefr-j.org/data/CEFRJ_wordlist_ver1.6.zip>

Source ZIP SHA-256:
`c837d2c00ab8954ed8db48e79afd8ef37099570295fec36950dbf9322303a37a`

The normalized file keeps only `headword` and `CEFR`, expands slash-separated
headword variants, lowercases them, and removes exact duplicate rows. No
definitions or examples are included.

`ecdict_exam_tags.tsv.gz` is a normalized extract of the `word` and `tag`
fields from an audited ECDICT snapshot. It keeps only `gk`, `cet4`, and `cet6`;
no definitions, translations, examples, pronunciations, Oxford data, or corpus
ranks are copied.

ECDICT source: <https://github.com/skywind3000/ECDICT>

Local ECDICT source SHA-256:
`2b5b40c2bdba04da0a51c8672e090f166987d5d895f32eb3fbfc5a516455fc75`

The repository publishes ECDICT under the MIT license and describes `tag` as
the space-separated exam labels `zk`/middle-school, `gk`/Gaokao, `cet4`, etc.
The source audit nevertheless records that ECDICT accumulated material from
multiple historical upstream sources. ResearchRamp therefore uses only the
minimal factual labels and retains the ECDICT MIT notice and this provenance
note in the distribution.

Generated asset SHA-256 values:

- `cefr_j_v1_6.tsv.gz`:
  `4b64192f94d96fa75c3848c922ef9eedc8c4720881a043b35fef59d3ce4e5af7`
- `ecdict_exam_tags.tsv.gz`:
  `7bc1df368c82960cec1ffabae24ae51c7e701154bfbc692b1ba66d8bbfcfb301`
