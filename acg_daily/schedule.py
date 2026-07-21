from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta


_DAILY_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_GROUP_MESSAGE_TYPE = "GroupMessage"
_QQ_GROUP_NUMBER_PATTERN = re.compile(r"^\d+$")
_AIOCQHTTP_PLATFORM = "aiocqhttp"


@dataclass(frozen=True)
class DailyPublishTime:
    """A validated local wall-clock time for one daily publication."""

    hour: int
    minute: int

    @property
    def text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def next_run_after(self, now: datetime) -> datetime:
        """Return the next occurrence in the timezone carried by ``now``."""

        candidate = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)

    def matches(self, now: datetime) -> bool:
        return now.hour == self.hour and now.minute == self.minute


def parse_daily_publish_time(value: object) -> DailyPublishTime | None:
    """Accept an optional strict ``HH:MM`` daily publication time."""

    text = str(value or "").strip()
    if not text:
        return None
    if not _DAILY_TIME_PATTERN.fullmatch(text):
        raise ValueError("定时发布时间必须为 24 小时制 HH:MM，例如 06:00")
    hour, minute = text.split(":")
    return DailyPublishTime(int(hour), int(minute))


def parse_publish_group_target(value: object) -> str:
    """Normalize a QQ group number or a configured platform-ID group session."""

    if not isinstance(value, str):
        raise ValueError("定时发布群聊白名单中的目标必须是文本")
    if value != value.strip():
        raise ValueError("定时发布群聊白名单中的目标不能包含首尾空白字符")
    target = value.strip()
    if not target:
        raise ValueError("定时发布群聊白名单中不能包含空目标")
    if _QQ_GROUP_NUMBER_PATTERN.fullmatch(target):
        return f"{_AIOCQHTTP_PLATFORM}:{_GROUP_MESSAGE_TYPE}:{target}"
    parts = target.split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError("定时发布群聊白名单必须是 QQ 群号或 平台ID:GroupMessage:群号 格式")
    _platform_id, message_type, session_id = parts
    if message_type != _GROUP_MESSAGE_TYPE:
        raise ValueError("定时发布群聊白名单只允许 GroupMessage 群聊目标")
    if not _QQ_GROUP_NUMBER_PATTERN.fullmatch(session_id):
        raise ValueError("定时发布群聊白名单中的群号必须仅包含数字")
    if any(part != part.strip() for part in parts):
        raise ValueError("定时发布群聊白名单中的目标不能包含首尾空白字符")
    return target


def resolve_publish_group_target(
    target: str,
    platforms: Iterable[tuple[str, str]],
) -> str:
    """Resolve a legacy adapter-type prefix to one active aiocqhttp platform ID."""

    configured_platform, message_type, group_id = target.split(":", 2)
    identities = tuple(platforms)
    exact_matches = [
        platform_id
        for platform_id, platform_name in identities
        if platform_id == configured_platform and platform_name == _AIOCQHTTP_PLATFORM
    ]
    if exact_matches:
        return target

    matching_ids = [
        platform_id
        for platform_id, platform_name in identities
        if platform_name == configured_platform
    ]
    if len(matching_ids) == 1:
        return f"{matching_ids[0]}:{message_type}:{group_id}"
    if not matching_ids:
        raise ValueError(f"未找到已加载的 aiocqhttp 平台实例「{configured_platform}」")
    raise ValueError(
        "检测到多个 aiocqhttp 平台实例（"
        + "、".join(matching_ids)
        + "），请在白名单填写实际平台ID:GroupMessage:群号",
    )


def parse_publish_group_whitelist(value: object) -> tuple[str, ...]:
    """Validate and deduplicate the group sessions eligible for daily publishing."""

    if not isinstance(value, list):
        raise ValueError("定时发布群聊白名单必须是列表")
    targets: list[str] = []
    for item in value:
        target = parse_publish_group_target(item)
        if target not in targets:
            targets.append(target)
    return tuple(targets)


@dataclass(frozen=True)
class DailyPublishSettings:
    """The complete, valid configuration required for proactive publishing."""

    time: DailyPublishTime
    targets: tuple[str, ...]

    def now(self) -> datetime:
        return datetime.now().astimezone()


def parse_daily_publish_settings(config: dict) -> DailyPublishSettings | None:
    """Return enabled scheduling settings, or ``None`` when the feature is disabled."""

    if not config.get("enable_daily_publish", False):
        return None

    publish_time = parse_daily_publish_time(config.get("daily_publish_time"))
    if publish_time is None:
        raise ValueError("已启用定时发布，但尚未填写每日发布时间")

    targets = parse_publish_group_whitelist(config.get("daily_publish_group_whitelist", []))
    if not targets:
        raise ValueError("已启用定时发布，但群聊白名单为空")

    return DailyPublishSettings(
        time=publish_time,
        targets=targets,
    )
