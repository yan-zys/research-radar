"""scheduler/holiday 节假日调度决策测试（纯函数，注入 holidays 集合）。"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "holiday", ROOT / "scheduler" / "holiday.py")
hd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hd)

# 模拟 2026 国庆假期：10-01 ~ 10-07 为节假日
NAT_DAY = {date(2026, 10, d) for d in range(1, 8)}


def test_daily_since_normal_day():
    """非节假日：--since 为昨天。"""
    assert hd.daily_since(date(2026, 7, 27), set()) == date(2026, 7, 26)


def test_daily_since_holiday_skips():
    """节假日当天：返回 None（跳过日报）。"""
    assert hd.daily_since(date(2026, 10, 1), NAT_DAY) is None
    assert hd.daily_since(date(2026, 10, 7), NAT_DAY) is None


def test_daily_since_merges_after_holiday():
    """节后首个非节假日：--since 取节前最后非节假日（9-30），覆盖整个空窗。"""
    assert hd.daily_since(date(2026, 10, 8), NAT_DAY) == date(2026, 9, 30)


def test_weekly_day_normal_friday():
    """普通周五（2026-07-24 是周五）：当天发送；周内其他日子不发。"""
    fri = date(2026, 7, 24)
    assert fri.weekday() == 4
    assert hd.weekly_day(fri, set()) == fri
    assert hd.weekly_day(date(2026, 7, 22), set()) is None  # 周三
    assert hd.weekly_day(date(2026, 7, 25), set()) is None  # 周六


def test_weekly_day_friday_holiday_shifts_earlier():
    """周五逢节假日（如 2026-10-02 周五在国庆假期内）：提前到 9-30（周三，
    10-01 也是节假日故连续提前）；发送日当天返回自身，节假日当天返回 None。"""
    assert date(2026, 10, 2).weekday() == 4
    send = date(2026, 9, 30)
    assert hd.weekly_day(send, NAT_DAY) == send
    assert hd.weekly_day(date(2026, 10, 2), NAT_DAY) is None
    assert hd.weekly_day(date(2026, 9, 29), NAT_DAY) is None  # 周二还不是发送日


def test_monthly_day_normal_month_end():
    """普通月底：最后一天发送；月中不发。"""
    assert hd.monthly_day(date(2026, 7, 31), set()) == date(2026, 7, 31)
    assert hd.monthly_day(date(2026, 7, 30), set()) is None
    assert hd.monthly_day(date(2026, 8, 1), set()) is None


def test_monthly_day_month_end_holiday_shifts_earlier():
    """月底逢节假日（2026-10-31 假设为节假日）：提前到 10-30。"""
    hol = {date(2026, 10, 31)}
    assert hd.monthly_day(date(2026, 10, 30), hol) == date(2026, 10, 30)
    assert hd.monthly_day(date(2026, 10, 31), hol) is None  # 当天是节假日，整体跳过


def test_monthly_day_february():
    """小月/闰年边界：2026 年 2 月 28 天。"""
    assert hd.monthly_day(date(2026, 2, 28), set()) == date(2026, 2, 28)
    assert hd.monthly_day(date(2026, 2, 27), set()) is None


def test_decide_holiday_skips_everything():
    """节假日：日报、周报、月报全部跳过。"""
    d = hd.decide(date(2026, 10, 2), NAT_DAY)  # 周五但在假期内
    assert d["skip"] is True and d["weekly"] is False and d["monthly"] is False


def test_decide_friday_sends_daily_and_weekly():
    """普通周五：日报 + 周报，无月报。"""
    d = hd.decide(date(2026, 7, 24), set())
    assert d["skip"] is False
    assert d["daily_since"] == date(2026, 7, 23)
    assert d["weekly"] is True and d["monthly"] is False


def test_decide_month_end_friday_sends_all_three():
    """月底恰逢周五（2026-07-31）：日报 + 周报 + 月报三封。"""
    assert date(2026, 7, 31).weekday() == 4
    d = hd.decide(date(2026, 7, 31), set())
    assert d["weekly"] is True and d["monthly"] is True
