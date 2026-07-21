import asyncio
import unittest

from acg_daily.title_lookup import (
    BangumiCandidate,
    MAX_LOOKUP_TITLES,
    bangumi_search_request,
    format_bangumi_candidates,
    lookup_references_for_text,
    normalized_lookup_titles,
    parse_bangumi_candidates,
    run_bangumi_lookups,
)


class TitleLookupTests(unittest.TestCase):
    def test_titles_are_normalized_deduplicated_and_bounded(self):
        titles = normalized_lookup_titles(["  Alpha   One  ", "Alpha One", "", *[f"Title {index}" for index in range(30)]])

        self.assertEqual(titles[:2], ["Alpha One", "Title 0"])
        self.assertEqual(len(titles), MAX_LOOKUP_TITLES)

    def test_titles_reject_non_string_values(self):
        titles = normalized_lookup_titles(["Original", True, 1, None, " Original "])

        self.assertEqual(titles, ["Original"])

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

    def test_bangumi_candidates_reject_non_string_fields_and_boolean_subject_types(self):
        candidates = parse_bangumi_candidates(
            {
                "data": [
                    {"name": None, "name_cn": "无效", "type": 2},
                    {"name": "Invalid", "name_cn": None, "type": 2},
                    {"name": "Invalid", "name_cn": "无效", "type": True},
                ],
            },
        )

        self.assertEqual(candidates, [])

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

    def test_lookup_references_identify_only_bangumi_candidates(self):
        references = lookup_references_for_text(
            "Original Anime gets a new trailer; Missing Title is also announced",
            {"Original Anime": [BangumiCandidate("Original Anime", "中文动画", 2, "")]},
        )

        self.assertEqual(references[0].query, "Original Anime")
        self.assertEqual(references[0].channel, "Bangumi")
        self.assertEqual(references[0].candidates, ("中文动画",))
        self.assertEqual(len(references), 1)


if __name__ == "__main__":
    unittest.main()
