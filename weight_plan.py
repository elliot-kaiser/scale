"""Weight trend analysis and calorie goal planning.

Uses logged body weight + daily calorie intake to estimate maintenance,
then projects intake needed to hit a goal weight by a target date.

Convention: ~3500 kcal ≈ 1 lb body weight.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

KCAL_PER_LB = 3500.0


def _parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _round1(value: float) -> float:
    return round(float(value), 1)


def _round0(value: float) -> int:
    return int(round(float(value)))


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares. Returns (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return 0.0, mean_y
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def analyze_weight_plan(
    day_rows: list,
    *,
    goal_weight_lbs: Optional[float] = None,
    goal_date: Optional[str] = None,
    lookback_days: int = 30,
    today: Optional[date] = None,
    excluded_dates: Optional[list] = None,
) -> dict:
    """Build trend + optional goal recommendation from daily_summary-style rows.

    Missed days are fine:
    - Weigh-ins use only logged scale readings (gaps OK; fit uses calendar span).
    - Calorie average uses only days with food logs (empty days are not treated as 0).

    excluded_dates: ISO dates omitted from the calorie average (incomplete tracking).
    Weigh-ins on those days still count toward the weight trend.
    """
    today = today or datetime.now().astimezone().date()
    window_start = today - timedelta(days=max(1, int(lookback_days)) - 1)
    excluded = set()
    for raw in excluded_dates or []:
        d = _parse_date(raw)
        if d:
            excluded.add(d.isoformat())

    by_date: dict[str, dict] = {}
    for row in day_rows or []:
        d = _parse_date(row.get("date"))
        if not d or d < window_start or d > today:
            continue
        key = d.isoformat()
        by_date[key] = {
            "date": key,
            "calories": float(row.get("calories") or 0),
            "meals_count": int(row.get("meals_count") or 0),
            "body_weight_lbs": (
                float(row["body_weight_lbs"])
                if row.get("body_weight_lbs") is not None
                else None
            ),
            "excluded": key in excluded,
        }

    weigh_ins = sorted(
        (
            (date.fromisoformat(r["date"]), r["body_weight_lbs"])
            for r in by_date.values()
            if r["body_weight_lbs"] is not None
        ),
        key=lambda item: item[0],
    )

    result = {
        "lookback_days": lookback_days,
        "window_start": window_start.isoformat(),
        "window_end": today.isoformat(),
        "weigh_in_count": len(weigh_ins),
        "excluded_dates": sorted(excluded),
        "excluded_count": len(excluded),
        "trend": None,
        "goal": None,
        "ok": False,
        "message": None,
    }

    if len(weigh_ins) < 2:
        result["message"] = (
            "Need at least 2 weigh-ins in the last "
            f"{lookback_days} days to estimate a trend."
        )
        return result

    first_day, first_lbs = weigh_ins[0]
    last_day, last_lbs = weigh_ins[-1]
    span_days = (last_day - first_day).days
    if span_days < 1:
        result["message"] = "Weigh-ins need to span more than one day."
        return result

    xs = [(d - first_day).days for d, _ in weigh_ins]
    ys = [w for _, w in weigh_ins]
    slope_per_day, _ = _linear_fit([float(x) for x in xs], ys)
    weekly_change = slope_per_day * 7.0
    total_change = last_lbs - first_lbs  # observed endpoints
    fit_change = slope_per_day * span_days

    # Average intake on days with food logs inside the weigh-in span.
    # Skip empty days and user-excluded (incomplete) days — never treat as 0 kcal.
    calorie_days = []
    skipped_empty = 0
    skipped_excluded = 0
    cursor = first_day
    while cursor <= last_day:
        key = cursor.isoformat()
        row = by_date.get(key)
        if key in excluded:
            skipped_excluded += 1
        elif not row or row["meals_count"] <= 0:
            skipped_empty += 1
        else:
            calorie_days.append(row["calories"])
        cursor += timedelta(days=1)

    if not calorie_days:
        result["message"] = (
            "No usable calorie logs between your weigh-ins — log meals, or un-exclude days."
        )
        result["trend"] = {
            "start_date": first_day.isoformat(),
            "end_date": last_day.isoformat(),
            "start_weight_lbs": _round1(first_lbs),
            "current_weight_lbs": _round1(last_lbs),
            "span_days": span_days,
            "observed_change_lbs": _round1(total_change),
            "trend_change_lbs": _round1(fit_change),
            "lbs_per_week": _round1(weekly_change),
            "avg_calories": None,
            "calorie_days": 0,
            "skipped_empty_days": skipped_empty,
            "skipped_excluded_days": skipped_excluded,
            "maintenance_calories": None,
            "intake_vs_maintenance": None,
        }
        return result

    avg_calories = sum(calorie_days) / len(calorie_days)
    # If losing weight (weekly_change < 0), intake was below maintenance
    deficit_per_day = (-weekly_change) * KCAL_PER_LB / 7.0
    maintenance = avg_calories + deficit_per_day

    trend = {
        "start_date": first_day.isoformat(),
        "end_date": last_day.isoformat(),
        "start_weight_lbs": _round1(first_lbs),
        "current_weight_lbs": _round1(last_lbs),
        "span_days": span_days,
        "observed_change_lbs": _round1(total_change),
        "trend_change_lbs": _round1(fit_change),
        "lbs_per_week": _round1(weekly_change),
        "avg_calories": _round0(avg_calories),
        "calorie_days": len(calorie_days),
        "skipped_empty_days": skipped_empty,
        "skipped_excluded_days": skipped_excluded,
        "maintenance_calories": _round0(maintenance),
        "intake_vs_maintenance": _round0(avg_calories - maintenance),
        "weigh_ins": [
            {"date": d.isoformat(), "weight_lbs": _round1(w)} for d, w in weigh_ins
        ],
    }
    result["trend"] = trend
    result["ok"] = True
    result["message"] = None

    if goal_weight_lbs is None or not goal_date:
        return result

    goal_lbs = float(goal_weight_lbs)
    target = _parse_date(goal_date)
    if not target:
        result["goal"] = {"error": "Invalid goal date."}
        return result
    if target <= today:
        result["goal"] = {"error": "Goal date must be in the future."}
        return result

    days_left = (target - today).days
    weeks_left = days_left / 7.0
    delta_lbs = goal_lbs - last_lbs
    needed_weekly = delta_lbs / weeks_left
    needed_delta_cal = needed_weekly * KCAL_PER_LB / 7.0
    goal_calories = maintenance + needed_delta_cal
    vs_maint = goal_calories - maintenance

    direction = "maintain"
    if abs(delta_lbs) < 0.05:
        direction = "maintain"
    elif delta_lbs > 0:
        direction = "gain"
    else:
        direction = "lose"

    result["goal"] = {
        "goal_weight_lbs": _round1(goal_lbs),
        "goal_date": target.isoformat(),
        "days_left": days_left,
        "weeks_left": _round1(weeks_left),
        "current_weight_lbs": _round1(last_lbs),
        "delta_lbs": _round1(delta_lbs),
        "direction": direction,
        "goal_calories": max(0, _round0(goal_calories)),
        "vs_maintenance": _round0(vs_maint),
        "mode": (
            "surplus" if vs_maint > 15 else "deficit" if vs_maint < -15 else "maintenance"
        ),
        "expected_lbs_per_week": _round1(needed_weekly),
        "maintenance_calories": _round0(maintenance),
    }
    return result


def weekly_review(
    day_rows: list,
    targets: dict,
    *,
    days: int = 7,
    today: Optional[date] = None,
    excluded_dates: Optional[list] = None,
    maintenance_calories: Optional[float] = None,
) -> dict:
    """Summarize the last N days: intake vs targets, weight change, adherence."""
    today = today or datetime.now().astimezone().date()
    days = max(1, min(int(days), 30))
    window_start = today - timedelta(days=days - 1)
    excluded = set()
    for raw in excluded_dates or []:
        d = _parse_date(raw)
        if d:
            excluded.add(d.isoformat())

    by_date = {}
    for row in day_rows or []:
        d = _parse_date(row.get("date"))
        if not d or d < window_start or d > today:
            continue
        by_date[d.isoformat()] = row

    calorie_vals = []
    protein_vals = []
    logged_days = 0
    excluded_logged = 0
    weigh_ins = []

    cursor = window_start
    while cursor <= today:
        key = cursor.isoformat()
        row = by_date.get(key) or {}
        meals_count = int(row.get("meals_count") or 0)
        cals = float(row.get("calories") or 0)
        if row.get("body_weight_lbs") is not None:
            weigh_ins.append((cursor, float(row["body_weight_lbs"])))
        if meals_count > 0:
            if key in excluded:
                excluded_logged += 1
            else:
                calorie_vals.append(cals)
                protein_vals.append(float(row.get("protein") or 0))
                logged_days += 1
        cursor += timedelta(days=1)

    target_cal = float((targets or {}).get("target_calories") or 0)
    target_p = float((targets or {}).get("target_protein") or 0)
    avg_cal = sum(calorie_vals) / len(calorie_vals) if calorie_vals else None
    avg_p = sum(protein_vals) / len(protein_vals) if protein_vals else None

    adherence = None
    if avg_cal is not None and target_cal > 0:
        # 100% = hit target exactly; clamp display later
        adherence = round(100.0 * avg_cal / target_cal, 0)

    weight_start = weigh_ins[0][1] if weigh_ins else None
    weight_end = weigh_ins[-1][1] if weigh_ins else None
    weight_delta = None
    if weight_start is not None and weight_end is not None and len(weigh_ins) >= 2:
        weight_delta = weight_end - weight_start

    vs_target = None if avg_cal is None else _round0(avg_cal - target_cal)
    vs_maint = None
    if avg_cal is not None and maintenance_calories is not None:
        vs_maint = _round0(avg_cal - float(maintenance_calories))

    on_track = None
    if vs_target is not None:
        if abs(vs_target) <= 150:
            on_track = "on_target"
        elif vs_target < 0:
            on_track = "under"
        else:
            on_track = "over"

    return {
        "window_days": days,
        "window_start": window_start.isoformat(),
        "window_end": today.isoformat(),
        "logged_days": logged_days,
        "excluded_days": excluded_logged,
        "avg_calories": _round0(avg_cal) if avg_cal is not None else None,
        "avg_protein": _round1(avg_p) if avg_p is not None else None,
        "target_calories": _round0(target_cal),
        "target_protein": _round1(target_p),
        "vs_target_calories": vs_target,
        "adherence_pct": adherence,
        "on_track": on_track,
        "weight_start_lbs": _round1(weight_start) if weight_start is not None else None,
        "weight_end_lbs": _round1(weight_end) if weight_end is not None else None,
        "weight_delta_lbs": _round1(weight_delta) if weight_delta is not None else None,
        "weigh_in_count": len(weigh_ins),
        "maintenance_calories": (
            _round0(maintenance_calories) if maintenance_calories is not None else None
        ),
        "vs_maintenance": vs_maint,
        "summary": _weekly_summary_line(
            logged_days, days, avg_cal, target_cal, weight_delta, vs_maint
        ),
    }


def _weekly_summary_line(logged_days, days, avg_cal, target_cal, weight_delta, vs_maint):
    if not logged_days:
        return "No food logged this week yet."
    parts = [f"{logged_days}/{days} days logged"]
    if avg_cal is not None and target_cal:
        diff = avg_cal - target_cal
        parts.append(
            f"avg {int(round(avg_cal))} kcal/day ({'+' if diff >= 0 else ''}{int(round(diff))} vs target)"
        )
    elif avg_cal is not None:
        parts.append(f"avg {int(round(avg_cal))} kcal/day")
    if weight_delta is not None:
        parts.append(f"weight {'+' if weight_delta >= 0 else ''}{round(weight_delta, 1)} lbs")
    if vs_maint is not None:
        if abs(vs_maint) < 50:
            parts.append("near estimated maintenance")
        elif vs_maint < 0:
            parts.append(f"~{abs(vs_maint)} kcal/day below maintenance")
        else:
            parts.append(f"~{vs_maint} kcal/day above maintenance")
    return " · ".join(parts)
