import unittest

from acg_daily.ranking import (
    RANKING_SOURCES,
    RANKING_SOURCE_ANIME_HACK,
    Ranking,
    RankingEntry,
    _anime_hack_ranking,
    parse_translated_ranking_items,
)


class RankingTests(unittest.TestCase):
    def test_anime_hack_is_a_builtin_top_ten_ranking_source(self):
        source = RANKING_SOURCES[RANKING_SOURCE_ANIME_HACK]

        self.assertEqual(source.url, "https://anime.eiga.com/ranking/program/")
        self.assertIn("Top 10", source.title)
        self.assertEqual(source.source, "Anime Hack")

    def test_anime_hack_ranking_uses_the_main_popular_anime_section(self):
        html = b"""
        <html><head><meta charset="utf-8"></head><body><section>
          <h1>\xe4\xba\xba\xe6\xb0\x97\xe3\x82\xa2\xe3\x83\x8b\xe3\x83\xa1\xe3\x83\xa9\xe3\x83\xb3\xe3\x82\xad\xe3\x83\xb3\xe3\x82\xb0</h1>
          <ol>
            <li>1\xe4\xbd\x8d <a href="/program/100/">Anime One</a><a href="/program/season/2026-summer/">Summer 2026</a></li>
            <li>2\xe4\xbd\x8d <a href="/program/101/">Anime Two</a><a href="/program/season/2026-summer/">Summer 2026</a></li>
          </ol>
        </section>
        <aside><h3>\xe8\xa9\xb1\xe9\xa1\x8c\xe3\x81\xae\xe3\x82\xa2\xe3\x83\x8b\xe3\x83\xa1</h3><ol><li>1\xe4\xbd\x8d <a href="/program/999/">\xe4\xb8\x8d\xe5\xba\x94\xe8\xaf\xbb\xe5\x8f\x96</a></li></ol></aside>
        </body></html>"""

        entries = _anime_hack_ranking(html)

        self.assertEqual(
            [(entry.rank, entry.title, entry.detail) for entry in entries],
            [(1, "Anime One", "Summer 2026"), (2, "Anime Two", "Summer 2026")],
        )

    def test_ranking_entries_are_bounded_to_top_ten(self):
        ranking = Ranking(
            "测试榜单",
            "测试来源",
            tuple(RankingEntry(index, f"作品{index}") for index in range(1, 13)),
        )

        self.assertEqual(len(ranking.entries[:10]), 10)

    def test_ranking_translation_requires_all_original_ranks_and_preserves_details(self):
        ranking = Ranking(
            "测试榜单",
            "测试来源",
            (RankingEntry(1, "Original One", "TV · 2026"), RankingEntry(2, "Original Two", "TV · 2025")),
        )

        translated = parse_translated_ranking_items(
            [{"rank": 2, "title": "作品二"}, {"rank": 1, "title": "作品一"}],
            ranking,
        )

        self.assertEqual(
            [(entry.rank, entry.title, entry.detail) for entry in translated.entries],
            [(1, "作品一", "TV · 2026"), (2, "作品二", "TV · 2025")],
        )
        with self.assertRaises(ValueError):
            parse_translated_ranking_items([{"rank": 1, "title": "作品一"}], ranking)


if __name__ == "__main__":
    unittest.main()
