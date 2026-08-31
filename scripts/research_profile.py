"""Validation for the user-confirmed research profile passed to the pipeline."""

from __future__ import annotations

from typing import Any


class ProfileValidationError(ValueError):
    """Raised before discovery when the conversational profile is incomplete."""


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_profile(profile: dict[str, Any]) -> None:
    errors: list[str] = []

    if profile.get("version") != 1:
        errors.append("version must be 1")
    if not _non_empty_text(profile.get("profile_id")):
        errors.append("profile_id is required")
    if profile.get("confirmed") is not True:
        errors.append("confirmed must be true after the user approves the summary")
    for key in ("user_statement", "research_summary"):
        if not _non_empty_text(profile.get(key)):
            errors.append(f"{key} is required")

    clarifications = profile.get("clarifications")
    if not isinstance(clarifications, list) or not 3 <= len(clarifications) <= 4:
        errors.append("clarifications must contain exactly 3 or 4 answered questions")
    else:
        for index, item in enumerate(clarifications, start=1):
            if not isinstance(item, dict):
                errors.append(f"clarifications[{index}] must be an object")
                continue
            if not _non_empty_text(item.get("question")):
                errors.append(f"clarifications[{index}].question is required")
            if not _non_empty_text(item.get("answer")):
                errors.append(f"clarifications[{index}].answer is required")

    queries = profile.get("search_queries")
    if not isinstance(queries, list) or not 3 <= len(queries) <= 24:
        errors.append("search_queries must contain 3 to 24 queries")
    else:
        seen_ids: set[str] = set()
        for index, item in enumerate(queries, start=1):
            if not isinstance(item, dict):
                errors.append(f"search_queries[{index}] must be an object")
                continue
            for key in ("id", "label", "query"):
                if not _non_empty_text(item.get(key)):
                    errors.append(f"search_queries[{index}].{key} is required")
            query_id = item.get("id")
            if _non_empty_text(query_id):
                if query_id in seen_ids:
                    errors.append(f"duplicate search query id: {query_id}")
                seen_ids.add(query_id)

    scope = profile.get("retrieval_scope")
    providers: list[str] = []
    if not isinstance(scope, dict):
        errors.append("retrieval_scope is required")
    else:
        if scope.get("confirmed") is not True:
            errors.append("retrieval_scope.confirmed must be true after user approval")
        for key in (
            "recent_from_year",
            "foundation_from_year",
            "foundation_before_year",
            "foundation_limit",
        ):
            if not isinstance(scope.get(key), int):
                errors.append(f"retrieval_scope.{key} must be an integer")
        configured_providers = scope.get("providers")
        if (
            not isinstance(configured_providers, list)
            or not configured_providers
            or not all(item in {"openalex", "arxiv"} for item in configured_providers)
            or len(configured_providers) != len(set(configured_providers))
        ):
            errors.append(
                "retrieval_scope.providers must be a unique non-empty list of "
                "openalex and/or arxiv"
            )
        else:
            providers = configured_providers

        if "openalex" in providers:
            openalex_filter = scope.get("openalex_primary_filter")
            if not isinstance(openalex_filter, dict):
                errors.append(
                    "retrieval_scope.openalex_primary_filter is required when OpenAlex is enabled"
                )
            else:
                if openalex_filter.get("level") not in {
                    "domain",
                    "field",
                    "subfield",
                    "topic",
                }:
                    errors.append(
                        "retrieval_scope.openalex_primary_filter.level must be domain, field, subfield, or topic"
                    )
                ids = openalex_filter.get("ids")
                labels = openalex_filter.get("labels")
                if (
                    not isinstance(ids, list)
                    or not ids
                    or not all(_non_empty_text(item) for item in ids)
                    or len(ids) != len(set(ids))
                ):
                    errors.append(
                        "retrieval_scope.openalex_primary_filter.ids must be a unique non-empty string list"
                    )
                if (
                    not isinstance(labels, list)
                    or not labels
                    or not all(_non_empty_text(item) for item in labels)
                ):
                    errors.append(
                        "retrieval_scope.openalex_primary_filter.labels must be a non-empty string list"
                    )
        required_lists = ["exclude_title_prefixes"]
        if "arxiv" in providers:
            required_lists.append("arxiv_categories")
        for key in required_lists:
            value = scope.get(key)
            if not isinstance(value, list) or not all(_non_empty_text(item) for item in value):
                errors.append(f"retrieval_scope.{key} must be a non-empty string list")

    arxiv_queries = profile.get("arxiv_search_queries")
    if "arxiv" not in providers and arxiv_queries in (None, []):
        arxiv_queries = []
    elif not isinstance(arxiv_queries, list) or not arxiv_queries:
        errors.append("arxiv_search_queries must contain at least one structured query")
    elif "arxiv" in providers:
        for index, item in enumerate(arxiv_queries, start=1):
            if not isinstance(item, dict):
                errors.append(f"arxiv_search_queries[{index}] must be an object")
                continue
            for key in ("id", "label"):
                if not _non_empty_text(item.get(key)):
                    errors.append(f"arxiv_search_queries[{index}].{key} is required")
            phrases = item.get("phrases")
            if not isinstance(phrases, list) or not all(
                _non_empty_text(phrase) for phrase in phrases
            ):
                errors.append(f"arxiv_search_queries[{index}].phrases is required")
            categories = item.get("categories")
            if not isinstance(categories, list) or not all(
                _non_empty_text(category) for category in categories
            ):
                errors.append(f"arxiv_search_queries[{index}].categories is required")
            if item.get("date_lane") not in {"recent", "foundation"}:
                errors.append(
                    f"arxiv_search_queries[{index}].date_lane must be recent or foundation"
                )

    if errors:
        raise ProfileValidationError("Invalid research profile:\n- " + "\n- ".join(errors))
