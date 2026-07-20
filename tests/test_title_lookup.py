import json
import asyncio
import unittest

from acg_daily.title_lookup import (
    MAX_LOOKUP_GROUPS,
    MAX_LOOKUP_QUERY_CHARS,
    MAX_LOOKUP_TITLES,
    compact_lookup_result,
    lookup_search_arguments,
    lookup_title_groups,
    normalized_lookup_titles,
    run_lookup_groups,
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
