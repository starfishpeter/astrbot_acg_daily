import json
import asyncio
import unittest

from acg_daily.title_lookup import (
    BangumiCandidate,
    MAX_LOOKUP_GROUPS,
    MAX_LOOKUP_QUERY_CHARS,
    MAX_LOOKUP_TITLES,
    compact_lookup_result,
    bangumi_search_request,
    format_bangumi_candidates,
    lookup_search_arguments,
    lookup_title_groups,
    normalized_lookup_titles,
    parse_bangumi_candidates,
    run_bangumi_lookups,
    run_lookup_groups,
    unresolved_bangumi_titles,
)


class TitleLookupTests(unittest.TestCase):
    def test_titles_are_normalized_deduplicated_and_bounded(self):
        titles = normalized_lookup_titles(["  Alpha   One  ", "Alpha One", "", *[f"Title {index}" for index in range(30)]])

        self.assertEqual(titles[:2], ["Alpha One", "Title 0"])
        self.assertEqual(len(titles), MAX_LOOKUP_TITLES)

    def test_titles_are_split_into_at_most_three_bounded_groups(self):
        titles = ["x" * 60 + str(index) for index in range(20)]

        groups = lookup_title_groups(titles, 70)

        self.assertLessEqual(len(groups), MAX_LOOKUP_GROUPS)
        self.assertTrue(all(sum(len(title) + 1 for title in group) <= 70 for group in groups))
        self.assertLess(sum(len(group) for group in groups), len(titles))

    def test_search_arguments_match_each_builtin_provider_schema(self):
        properties = {
            "max_results": {},
            "count": {},
            "limit": {},
            "top_k": {},
            "num_results": {},
        }

        tavily = lookup_search_arguments(["Title"], "web_search_tavily", {"properties": {"max_results": {}}})
        baidu = lookup_search_arguments(["Title"], "web_search_baidu", {"properties": {"top_k": {}}})
        all_limits = [
            lookup_search_arguments(["Title"], "other", {"properties": {name: {}}})
            for name in properties
        ]

        self.assertEqual(tavily["max_results"], 5)
        self.assertEqual(baidu, {"query": "Title 中文译名", "top_k": 4})
        self.assertEqual([{key for key in value if key != "query"}.pop() for value in all_limits], list(properties))

    def test_search_result_is_compacted_to_four_short_snippets(self):
        response = json.dumps(
            {
                "results": [
                    {"title": f" Title {index} ", "snippet": " detail " * 100, "url": "https://example.com"}
                    for index in range(5)
                ]
            }
        )

        result = compact_lookup_result(response)

        self.assertEqual(result.count("- Title"), 4)
        self.assertNotIn("https://example.com", result)
        self.assertLessEqual(max(len(line) for line in result.splitlines()), 424)

    def test_search_failure_or_unstructured_result_is_safe(self):
        self.assertEqual(compact_lookup_result(RuntimeError("failed")), "")
        self.assertEqual(compact_lookup_result("x" * 1300), "x" * 1200)

    def test_bangumi_candidates_keep_all_supported_subject_types_for_agent_judgment(self):
        candidates = parse_bangumi_candidates(
            {
                "data": [
                    {"name": "Original Book", "name_cn": "中文漫画", "type": 1, "date": "2024-01-01"},
                    {"name": "Original Anime", "name_cn": "中文动画", "type": 2, "date": "2025-01-01"},
                    {"name": "Original Game", "name_cn": "中文游戏", "type": 4},
                    {"name": "No Chinese", "name_cn": "", "type": 2},
                    {"name": "Music", "name_cn": "音乐", "type": 3},
                ]
            }
        )

        self.assertEqual(
            candidates,
            [
                BangumiCandidate("Original Book", "中文漫画", 1, "2024-01-01"),
                BangumiCandidate("Original Anime", "中文动画", 2, "2025-01-01"),
                BangumiCandidate("Original Game", "中文游戏", 4, ""),
            ],
        )
        rendered = format_bangumi_candidates({"Original": candidates})
        self.assertIn("[书籍/漫画] Original Book -> 中文漫画", rendered)
        self.assertIn("[动画] Original Anime -> 中文动画", rendered)
        self.assertIn("[游戏] Original Game -> 中文游戏", rendered)

    def test_bangumi_request_uses_bearer_header_and_all_supported_subject_types(self):
        url, headers, payload = bangumi_search_request("Original title", "private-token")

        self.assertEqual(url, "https://api.bgm.tv/v0/search/subjects")
        self.assertEqual(headers["Authorization"], "Bearer private-token")
        self.assertNotIn("private-token", str(payload))
        self.assertEqual(payload, {"keyword": "Original title", "filter": {"type": [1, 2, 4]}, "limit": 5})

    def test_bangumi_lookup_tolerates_per_title_failures(self):
        async def lookup(title):
            if title == "failed":
                raise RuntimeError("network failed")
            return [BangumiCandidate(title, "中文名", 2, "")]

        matches = asyncio.run(run_bangumi_lookups(["works", "failed"], lookup))

        self.assertEqual(matches["works"], [BangumiCandidate("works", "中文名", 2, "")])
        self.assertEqual(matches["failed"], [])

    def test_bangumi_lookups_limit_concurrency_to_five_titles(self):
        active = 0
        peak = 0
        release = asyncio.Event()

        async def lookup(_title):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 5:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=0.2)
            active -= 1
            return []

        asyncio.run(run_bangumi_lookups([f"title-{index}" for index in range(7)], lookup))

        self.assertEqual(peak, 5)

    def test_any_bangumi_candidate_leaves_ambiguous_selection_to_the_agent_without_web_fallback(self):
        titles = ["same title", "unmatched title"]
        matches = {
            "same title": [
                BangumiCandidate("Same Title", "同名动画", 2, "2025-01-01"),
                BangumiCandidate("Same Title", "同名漫画", 1, "2024-01-01"),
            ],
            "unmatched title": [],
        }

        self.assertEqual(unresolved_bangumi_titles(titles, matches), ["unmatched title"])

    def test_lookup_groups_run_concurrently_and_tolerate_partial_failure(self):
        started = 0
        all_started = asyncio.Event()

        async def search(group):
            nonlocal started
            started += 1
            if started == 2:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.2)
            if group == ["failed"]:
                raise RuntimeError("search failed")
            return group[0]

        results = asyncio.run(run_lookup_groups([["first"], ["failed"]], search))

        self.assertEqual(results[0], "first")
        self.assertIsInstance(results[1], RuntimeError)


if __name__ == "__main__":
    unittest.main()
