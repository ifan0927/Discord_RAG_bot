from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class MemberIdentity:
    user_id: int
    display_name: str
    source: str


@dataclass(frozen=True)
class MemberIdentityContext:
    caller: MemberIdentity
    mentioned_members: tuple[MemberIdentity, ...] = ()
    unknown_member_ids: tuple[int, ...] = ()
    status: tuple[str, ...] = ()

    def display_name_for(self, user_id: int) -> str:
        if self.caller.user_id == user_id:
            return self.caller.display_name
        for member in self.mentioned_members:
            if member.user_id == user_id:
                return member.display_name
        return fallback_display_name(user_id)


@dataclass(frozen=True)
class TimeWindow:
    start_at: datetime
    end_at: datetime

    def log_value(self) -> dict[str, str]:
        return {
            "start_at": self.start_at.astimezone(timezone.utc).isoformat(),
            "end_at": self.end_at.astimezone(timezone.utc).isoformat(),
        }


@dataclass(frozen=True)
class RetrievalIntent:
    target_author_ids: tuple[int, ...] = ()
    target_time_window: TimeWindow | None = None


MENTION_RE = re.compile(r"<@!?(\d+)>")
AUTHOR_RE = re.compile(r"author:(\d+)")
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])(?!\d)")
MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/](0?[1-9]|1[0-2])(?![-/\d])")
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})年?(?![-/\d])")


def fallback_display_name(user_id: int) -> str:
    return f"member:{user_id}"


def build_member_identity_context(
    *,
    caller_id: int,
    caller_display_name: str | None,
    mentioned_members: tuple[MemberIdentity, ...] = (),
    unknown_member_ids: tuple[int, ...] = (),
    status: tuple[str, ...] = (),
) -> MemberIdentityContext:
    caller = MemberIdentity(
        caller_id,
        _clean_display_name(caller_display_name) or fallback_display_name(caller_id),
        "payload" if caller_display_name else "fallback",
    )
    deduped = _dedupe_members(mentioned_members)
    return MemberIdentityContext(
        caller=caller,
        mentioned_members=deduped,
        unknown_member_ids=tuple(dict.fromkeys(unknown_member_ids)),
        status=status,
    )


def parse_mentioned_user_ids(raw_content: str, *, bot_user_id: int) -> tuple[int, ...]:
    ids = []
    for match in MENTION_RE.finditer(raw_content):
        user_id = int(match.group(1))
        if user_id != bot_user_id:
            ids.append(user_id)
    return tuple(dict.fromkeys(ids))


def build_retrieval_intent(
    user_query: str,
    identity: MemberIdentityContext,
    now: datetime,
) -> RetrievalIntent:
    target_author_ids = _target_author_ids(user_query, identity)
    return RetrievalIntent(
        target_author_ids=target_author_ids,
        target_time_window=parse_time_window(user_query, now),
    )


def parse_time_window(user_query: str, now: datetime) -> TimeWindow | None:
    query = user_query.strip()
    local_now = now.astimezone(TAIPEI)
    local_today = local_now.date()

    date_match = DATE_RE.search(query)
    if date_match:
        year, month, day = (int(part) for part in date_match.groups())
        try:
            start = datetime(year, month, day, tzinfo=TAIPEI)
        except ValueError:
            return None
        return _local_window(start, start + timedelta(days=1))

    month_match = MONTH_RE.search(query)
    if month_match:
        year, month = (int(part) for part in month_match.groups())
        start = datetime(year, month, 1, tzinfo=TAIPEI)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=TAIPEI)
        else:
            end = datetime(year, month + 1, 1, tzinfo=TAIPEI)
        return _local_window(start, end)

    year_match = YEAR_RE.search(query)
    if year_match:
        year = int(year_match.group(1))
        return _local_window(
            datetime(year, 1, 1, tzinfo=TAIPEI),
            datetime(year + 1, 1, 1, tzinfo=TAIPEI),
        )

    if "前天" in query:
        return _day_window(local_today - timedelta(days=2))
    if "昨天" in query:
        return _day_window(local_today - timedelta(days=1))
    if "今天" in query:
        return _day_window(local_today)
    if "本週" in query or "這週" in query:
        start_day = local_today - timedelta(days=local_today.weekday())
        return _local_window(
            datetime.combine(start_day, datetime.min.time(), tzinfo=TAIPEI),
            datetime.combine(start_day + timedelta(days=7), datetime.min.time(), tzinfo=TAIPEI),
        )

    return None


def render_member_identity_block(identity: MemberIdentityContext) -> str:
    lines = [
        "目前互動身份:",
        f"- caller: {identity.caller.display_name} ({identity.caller.user_id})",
    ]
    if identity.mentioned_members:
        lines.append("- mentioned members:")
        for member in identity.mentioned_members:
            lines.append(f"  - {member.display_name} ({member.user_id})")
    if identity.unknown_member_ids:
        lines.append("- unknown mentioned member ids:")
        for user_id in identity.unknown_member_ids:
            lines.append(f"  - {user_id}")
    return "\n".join(lines)


def render_author_names(text: str, identity: MemberIdentityContext) -> str:
    def replace(match: re.Match[str]) -> str:
        user_id = int(match.group(1))
        return f"author:{identity.display_name_for(user_id)} ({user_id})"

    return AUTHOR_RE.sub(replace, text)


def identity_status(identity: MemberIdentityContext) -> tuple[str, ...]:
    statuses = list(identity.status)
    if identity.caller.source == "fallback":
        statuses.append("caller_fallback")
    for member in identity.mentioned_members:
        statuses.append(f"mentioned_{member.source}")
    if identity.unknown_member_ids:
        statuses.append("unknown_member_fallback")
    return tuple(dict.fromkeys(statuses))


def _target_author_ids(
    user_query: str, identity: MemberIdentityContext
) -> tuple[int, ...]:
    ids = [member.user_id for member in identity.mentioned_members]
    query = user_query.casefold()
    caller_name = identity.caller.display_name.casefold()
    if caller_name != fallback_display_name(identity.caller.user_id).casefold():
        history_terms = ("之前", "以前", "上次", "有說", "講過", "提過")
        if caller_name in query and any(term in user_query for term in history_terms):
            ids.append(identity.caller.user_id)
    return tuple(dict.fromkeys(ids))


def _day_window(day) -> TimeWindow:
    start = datetime.combine(day, datetime.min.time(), tzinfo=TAIPEI)
    return _local_window(start, start + timedelta(days=1))


def _local_window(start: datetime, end: datetime) -> TimeWindow:
    return TimeWindow(start.astimezone(timezone.utc), end.astimezone(timezone.utc))


def _clean_display_name(value: str | None) -> str:
    return (value or "").strip()


def _dedupe_members(members: tuple[MemberIdentity, ...]) -> tuple[MemberIdentity, ...]:
    deduped: dict[int, MemberIdentity] = {}
    for member in members:
        deduped.setdefault(member.user_id, member)
    return tuple(deduped.values())
