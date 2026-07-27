#!/usr/bin/env python
"""节假日感知调度决策（中国法定节假日，数据来自 chinese-calendar 包）。

规则（用户确认）：
- 日报：每天发送，法定节假日跳过；节假日后的第一个非节假日发送合并日报，
  爬虫 --since 取「今天之前最近一个非节假日」，自然覆盖节假日期间文献。
- 周报：每周五发送；若周五是节假日则提前到前一个非节假日（可连续提前）。
- 月报：每月最后一天发送；若月底是节假日则提前到前一个非节假日。

纯函数可注入 holidays 集合便于测试；CLI 输出 shell 可 eval 的决策行。
"""
import calendar
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_holidays() -> set:
    """法定节假日集合（chinese_calendar.holidays 的键，含调休休息日，
    不含普通周末）。包缺失或数据未覆盖时返回空集（退化为无节假日）。"""
    try:
        import chinese_calendar as cc
        return set(cc.holidays.keys())
    except Exception:
        return set()


def daily_since(today: date, holidays: set):
    """今天是否发日报：节假日返回 None（跳过）；否则返回爬虫 --since 日期，
    即今天之前最近一个非节假日（合并覆盖节假日空窗）。"""
    if today in holidays:
        return None
    d = today - timedelta(days=1)
    while d in holidays:
        d -= timedelta(days=1)
    return d


def weekly_day(today: date, holidays: set):
    """本周周报发送日：周五（weekday=4）起向前避开节假日。今天即发送日则返回该日期。"""
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    d = friday
    while d in holidays:
        d -= timedelta(days=1)
    return d if d == today else None


def monthly_day(today: date, holidays: set):
    """本月月报发送日：月末起向前避开节假日。今天即发送日则返回该日期。"""
    last = date(today.year, today.month,
                calendar.monthrange(today.year, today.month)[1])
    d = last
    while d in holidays:
        d -= timedelta(days=1)
    return d if d == today else None


def decide(today: date, holidays: set) -> dict:
    """汇总今日决策：skip / daily_since / weekly / monthly。"""
    since = daily_since(today, holidays)
    return {
        "skip": since is None,
        "daily_since": since,
        "weekly": since is not None and weekly_day(today, holidays) is not None,
        "monthly": since is not None and monthly_day(today, holidays) is not None,
    }


def main():
    today = date.today()
    d = decide(today, load_holidays())
    if d["skip"]:
        print("SKIP=1")
        print(f"# {today} 是法定节假日，日报/周报/月报全部跳过", file=sys.stderr)
        return
    print(f"DAILY_SINCE={d['daily_since'].isoformat()}")
    print(f"WEEKLY={1 if d['weekly'] else 0}")
    print(f"MONTHLY={1 if d['monthly'] else 0}")


if __name__ == "__main__":
    main()
