# Confirmed research profile

The agent creates this JSON only after asking 3–4 follow-up questions and receiving explicit confirmation of its plain-language research summary.

```json
{
  "version": 1,
  "profile_id": "short-stable-topic-id-v1",
  "confirmed": true,
  "user_statement": "The user's original description, without silently narrowing it.",
  "research_summary": "The scope the user explicitly confirmed.",
  "clarifications": [
    {"question": "Question asked in chat", "answer": "User's answer"},
    {"question": "Question asked in chat", "answer": "User's answer"},
    {"question": "Question asked in chat", "answer": "User's answer"}
  ],
  "search_queries": [
    {"id": "q01", "label": "Human-readable angle", "query": "cross-disciplinary scholarly query"},
    {"id": "q02", "label": "Human-readable angle", "query": "cross-disciplinary scholarly query"},
    {"id": "q03", "label": "Human-readable angle", "query": "cross-disciplinary scholarly query"}
  ],
  "retrieval_scope": {
    "confirmed": true,
    "providers": ["openalex", "arxiv"],
    "openalex_primary_filter": {
      "level": "field",
      "ids": ["17"],
      "labels": ["Computer Science"]
    },
    "language": "en",
    "recent_from_year": 2016,
    "foundation_from_year": 1990,
    "foundation_before_year": 2016,
    "foundation_limit": 10,
    "arxiv_categories": ["cs.CL", "cs.AI"],
    "exclude_title_prefixes": ["comment on", "reply to", "erratum", "correction", "withdrawn"]
  },
  "arxiv_search_queries": [
    {
      "id": "ax01",
      "label": "Human-readable angle",
      "phrases": ["short scholarly phrase", "accepted synonym"],
      "categories": ["cs.CL", "cs.AI"],
      "date_lane": "recent"
    }
  ]
}
```

Rules:

- `clarifications` must contain exactly 3 or 4 questions that were actually answered.
- Generate 3–24 complementary search queries from the confirmed scope. This query expansion enables discovery; it is not a paper scoring or filtering stage.
- Before creating the file, show the user one compact retrieval-scope proposal containing the included disciplines/categories, the recent-year boundary, and whether a small older-foundation lane is enabled. `retrieval_scope.confirmed` becomes true only after the user approves or corrects that proposal.
- The ordinary default is English-language public full text, the most recent ten years, and at most 10 older foundational papers. Adapt those values to the research area and the user's answer; do not silently impose them when the user requests a different historical scope.
- Choose providers during initialization from the confirmed discipline, not during a later weekly run. Use OpenAlex as the cross-disciplinary backbone. Add arXiv only when the confirmed discipline has meaningful arXiv category coverage. Do not use Semantic Scholar.
- When OpenAlex is enabled, resolve one primary OpenAlex taxonomy boundary and store it in `openalex_primary_filter`. Choose the narrowest level that still represents the whole confirmed research program: `topic`, `subfield`, `field`, or `domain`. Multiple IDs at that same level are OR alternatives. Weekly and initial searches must send this as a `primary_topic.*` filter to OpenAlex; labels are kept so the host agent and the audit can see what was constrained.
- Generate 4–12 structured `arxiv_search_queries` only when `retrieval_scope.providers` contains `arxiv`. `phrases` must be established two- or three-word scholarly concepts, `categories` must express the confirmed disciplinary boundary, and `date_lane` is either `recent` or `foundation`. Otherwise use an empty list.
- Never use an unqualified `all:` query. The acquisition script searches each phrase only in title or abstract and combines it with the confirmed category and date boundaries.
- Preserve the user's boundaries and terminology. Add adjacent scholarly terminology only when it expresses the same confirmed problem.
- Do not set `confirmed` to `true` based only on the agent's own summary.
