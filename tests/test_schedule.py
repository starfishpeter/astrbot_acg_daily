import unittest
from datetime import datetime, timezone

from acg_daily.schedule import (
    DailyPublishTime,
    parse_daily_publish_settings,
    parse_daily_publish_time,
    parse_publish_group_whitelist,
    parse_publish_timezone,
)


class DailyPublishScheduleTests(unittest.TestCase):
    def test_daily_time_is_strict_hh_mm_and_calculates_next_run(self):
        publish_time = parse_daily_publish_time("06:00")

        self.assertEqual(publish_time, DailyPublishTime(6, 0))
        self.assertTrue(publish_time.matches(datetime(2026, 7, 20, 6, 0, 14, tzinfo=timezone.utc)))
        self.assertEqual(
            publish_time.next_run_after(datetime(2026, 7, 20, 6, 1, tzinfo=timezone.utc)),
            datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            publish_time.next_run_after(datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)),
            datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            parse_daily_publish_time("6:00")
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            parse_daily_publish_time("06:00:00")

    def test_publish_whitelist_allows_only_group_message_origins(self):
        self.assertEqual(
            parse_publish_group_whitelist(
                [
                    "aiocqhttp:GroupMessage:123456789",
                    "aiocqhttp:GroupMessage:123456789",
                    "aiocqhttp:GroupMessage:987654321",
                ]
            ),
            (
                "aiocqhttp:GroupMessage:123456789",
                "aiocqhttp:GroupMessage:987654321",
            ),
        )
        for whitelist in (
            "aiocqhttp:GroupMessage:123456789",
            ["123456789"],
            ["aiocqhttp:FriendMessage:123"],
            ["aiocqhttp:GroupMessage:"],
            ["aiocqhttp:GroupMessage:group:123"],
            [" aiocqhttp:GroupMessage:123"],
        ):
            with self.subTest(whitelist=whitelist), self.assertRaises(ValueError):
                parse_publish_group_whitelist(whitelist)

    def test_enabled_schedule_requires_all_settings_and_accepts_timezone(self):
        self.assertIsNone(parse_daily_publish_settings({"enable_daily_publish": False}))
        with self.assertRaisesRegex(ValueError, "每日发布时间"):
            parse_daily_publish_settings({"enable_daily_publish": True})
        with self.assertRaisesRegex(ValueError, "群聊白名单为空"):
            parse_daily_publish_settings({"enable_daily_publish": True, "daily_publish_time": "06:00"})

        settings = parse_daily_publish_settings(
            {
                "enable_daily_publish": True,
                "daily_publish_time": "06:00",
                "daily_publish_group_whitelist": [
                    "aiocqhttp:GroupMessage:123456789",
                    "aiocqhttp:GroupMessage:987654321",
                ],
                "daily_publish_timezone": "Asia/Shanghai",
            }
        )

        self.assertEqual(settings.time.text, "06:00")
        self.assertEqual(
            settings.targets,
            ("aiocqhttp:GroupMessage:123456789", "aiocqhttp:GroupMessage:987654321"),
        )
        self.assertEqual(settings.timezone.key, "Asia/Shanghai")
        with self.assertRaisesRegex(ValueError, "时区无效"):
            parse_publish_timezone("Moon/Base")


if __name__ == "__main__":
    unittest.main()
