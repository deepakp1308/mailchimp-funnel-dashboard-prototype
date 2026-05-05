"""End-to-end data build for the FY26 Web Scorecard 4-stage funnel dashboard.

Runs all required BigQuery extractions and writes:
  - data/funnel-cube.json   (4 windows x dim cube of visits/activations/trials/paid)
  - data/weekly-trend.json  (44 ISO weeks current + 364-day shifted prior overlay)

Stitching: GA4 supplies Visits + Activations at full granularity. Trials and Paid
come from BI weekly aggregates that lack landing-page / channel context, so we
allocate the BI totals (per period x country) proportionally across the GA-side
(landing_family, channel, device, new_returning) Visits share so multi-select
filters remain additive.

This script depends on application-default credentials for BigQuery via
`google-cloud-bigquery`.
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

from google.cloud import bigquery  # noqa: E402


PROJECT = "mc-business-intelligence"
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# ---------------------------------------------------------------------------
# Window + dimension config
# ---------------------------------------------------------------------------

# As-of date. We anchor to the last day of the last fully-complete BI week so
# the 30/90 day windows do not include a partial weekly aggregate (BI weeks
# start on Sunday; we end on the prior Saturday). Today is 2026-05-04 (Mon),
# last complete BI week is Sun 2026-04-26 .. Sat 2026-05-02, so AS_OF = May 2.
AS_OF = date(2026, 5, 2)

WINDOWS = {
    "current_30d":  (AS_OF - timedelta(days=29), AS_OF),
    "prior_yr_30d": (AS_OF - timedelta(days=29) - timedelta(days=365), AS_OF - timedelta(days=365)),
    "current_90d":  (AS_OF - timedelta(days=89), AS_OF),
    "prior_yr_90d": (AS_OF - timedelta(days=89) - timedelta(days=365), AS_OF - timedelta(days=365)),
}

# Trend window: 44 ISO weeks back from the week containing AS_OF.
TREND_END_WEEK = AS_OF - timedelta(days=AS_OF.weekday())  # Monday of current week
TREND_START_WEEK = TREND_END_WEEK - timedelta(weeks=43)
TREND_PRIOR_OFFSET_DAYS = 364  # 52 weeks, keeps day-of-week aligned

ACQ_PAGE_GROUPS = (
    "homepage", "marketing_pricing_page", "solutions",
    "solutions_pages_email_marketing", "solutions_pages_sms_marketing",
    "solutions_pages_marketing_automation", "solutions_pages_templates",
    "signup_start_page", "other_switch_to_mailchimp",
    "overview_pages", "sales", "contact_pages",
    "landing_pages",  # paid-media landing pages, added to align with Tableau scope
)

TOP_COUNTRIES = [
    "United States", "Canada", "United Kingdom", "India", "Mexico",
    "China", "Spain", "Australia", "Germany", "Japan",
    "Argentina", "Saudi Arabia", "Ireland", "Brazil", "France",
    "Italy", "Netherlands", "Singapore", "Thailand", "Malaysia",
]

# Map BI country_group -> the GA-side country bucket. Aggregates collapse to "Other".
COUNTRY_GROUP_TO_BUCKET = {
    "United States": "United States",
    "Canada": "Canada",
    "United Kingdom": "United Kingdom",
    "Australia": "Australia",
    "Ireland": "Ireland",
    "Netherlands": "Netherlands",
    # BI groups without a 1:1 country in our top-20 land in "Other".
    "Belgium": "Other",
    "New Zealand": "Other",
    "Nordics": "Other",
    "Tier 1 Develop": "Other",
    "Tier 2 Develop": "Other",
    "ROW": "Other",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bq() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def _run(sql: str):
    return list(_bq().query(sql).result())


def _date(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# 1. Cube: GA visits + activations across all 4 windows in one scan.
# ---------------------------------------------------------------------------

def build_cube_ga() -> list[dict]:
    acq_list = ",".join(f"'{x}'" for x in ACQ_PAGE_GROUPS)
    country_list = ",".join(f"'{c}'" for c in TOP_COUNTRIES)

    # Two contiguous date ranges cover all four windows.
    cur_start, cur_end = WINDOWS["current_90d"]
    pri_start, pri_end = WINDOWS["prior_yr_90d"]

    sql = f"""
    WITH events AS (
      SELECT
        user_pseudo_id, session_id, event_name, event_timestamp, event_date,
        user_id, hostname, page_group, page_path, geo_country, device_category,
        ga_session_number, traffic_source_source, traffic_source_medium
      FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
      WHERE (event_date BETWEEN DATE('{_date(pri_start)}') AND DATE('{_date(pri_end)}')
          OR event_date BETWEEN DATE('{_date(cur_start)}') AND DATE('{_date(cur_end)}'))
        AND (hostname='mailchimp.com' OR (event_name='sign_up' AND hostname LIKE '%mailchimp.com'))
    ),
    -- A "session" here is a marketing-site session on hostname=mailchimp.com.
    -- Entry attributes are taken from the FIRST event in the session that has a
    -- non-null page_path. In 2026 GA4 began emitting many ambient/automation
    -- events with a NULL page_path; using event_timestamp ASC LIMIT 1 directly
    -- (the prior logic) often picked one of these and then the carve-out
    -- silently dropped the session, deflating Visits by 60-80% in recent months.
    -- HAVING first_page IS NOT NULL excludes sessions that never visited any
    -- page (pure background events / bots).
    mc_sessions AS (
      SELECT user_pseudo_id, session_id,
        MIN(event_date) AS session_date,
        MAX(IF(ga_session_number=1,1,0)) AS is_new,
        ARRAY_AGG(
          IF(page_path IS NOT NULL,
             STRUCT(page_group, traffic_source_source, traffic_source_medium, geo_country, device_category),
             NULL)
          IGNORE NULLS
          ORDER BY event_timestamp ASC LIMIT 1
        )[SAFE_OFFSET(0)] AS first_page
      FROM events WHERE hostname='mailchimp.com'
      GROUP BY 1,2
      HAVING first_page IS NOT NULL
    ),
    session_dims AS (
      SELECT
        user_pseudo_id, session_id, session_date,
        IF(is_new=1,'new','returning') AS new_returning,
        IF(IFNULL(first_page.geo_country,'Other') IN ({country_list}), first_page.geo_country, 'Other') AS geo_country,
        CASE LOWER(IFNULL(first_page.device_category,''))
          WHEN 'mobile' THEN 'mobile' WHEN 'desktop' THEN 'desktop' WHEN 'tablet' THEN 'tablet'
          ELSE 'desktop'
        END AS device,
        CASE
          WHEN LOWER(first_page.traffic_source_medium) IN ('cpc','ppc','paidsearch','paid-search','paid_search') THEN 'Paid Search'
          WHEN LOWER(first_page.traffic_source_medium) IN ('paid_social','paidsocial','paid-social','social-paid','social_paid','cpm') THEN 'Paid Social'
          WHEN LOWER(first_page.traffic_source_medium) IN ('organic','seo') THEN 'Organic Search'
          WHEN LOWER(first_page.traffic_source_medium) IN ('social','social-organic','social_organic','organic_social','organic-social') THEN 'Organic Social'
          WHEN LOWER(first_page.traffic_source_medium) IN ('email','newsletter') THEN 'Email'
          WHEN LOWER(first_page.traffic_source_medium) IN ('affiliate','affiliates') THEN 'Affiliate'
          WHEN LOWER(first_page.traffic_source_medium) = 'referral' THEN 'Referral'
          WHEN LOWER(first_page.traffic_source_medium) IN ('display','banner','cpv') THEN 'Display / Other'
          WHEN LOWER(first_page.traffic_source_medium) IN ('','(none)','none','direct')
            OR first_page.traffic_source_medium IS NULL
            OR LOWER(first_page.traffic_source_source) IN ('(direct)','direct') THEN 'Direct'
          ELSE 'Display / Other'
        END AS channel,
        CASE
          WHEN first_page.page_group='homepage' THEN 'Homepage'
          WHEN first_page.page_group='marketing_pricing_page' THEN 'Pricing'
          WHEN first_page.page_group='solutions_pages_email_marketing' THEN 'Email Marketing solution'
          WHEN first_page.page_group='solutions_pages_sms_marketing' THEN 'SMS Marketing solution'
          WHEN first_page.page_group='solutions_pages_marketing_automation' THEN 'Marketing Automation solution'
          WHEN first_page.page_group IN ('solutions','solutions_pages_templates','overview_pages','sales','contact_pages','expert_directory','onboarding_services','personalize') THEN 'Other Solutions'
          WHEN first_page.page_group='feature_pages' OR first_page.page_group LIKE 'features_pages_%' THEN 'Other Feature pages'
          WHEN first_page.page_group IN ('resources_pages','resources_pages_email','resources_pages_benchmarks','resources_pages_home','resources_pages_deliverability','marketing_glossary_pages','mailchimp_presents','mailchimp_story_pages') THEN 'SEO Resources'
          WHEN first_page.page_group='knowledge_base_pages' OR first_page.page_group LIKE 'help_pages_%' THEN 'Knowledge Base / Help'
          WHEN first_page.page_group LIKE 'integrations_pages%' THEN 'Integrations'
          WHEN first_page.page_group='other_switch_to_mailchimp' THEN 'Switch-to / Compete'
          WHEN first_page.page_group='signup_start_page' THEN 'Direct to Signup'
          WHEN first_page.page_group='landing_pages' THEN 'Paid Landing Pages'
          ELSE 'Other'
        END AS landing_family,
        IF(first_page.page_group IN ({acq_list}),1,0) AS is_acq
      FROM mc_sessions
    ),
    -- Visits = all marketing-site sessions whose entry page_group is in the
    -- acquisition carve-out (homepage / pricing / solutions / signup_start /
    -- sales / contact / overview / templates / switch-to / paid landing pages).
    -- We deliberately DO NOT filter by user_id IS NULL: in 2026 GA4 began
    -- persisting user_id across sessions for any visitor who once authenticated
    -- on the device, so any_uid=0 now excludes 40-60% of legitimate prospect
    -- traffic on the public marketing site (was 10-15% in FY26 Q1 2025). The
    -- hostname='mailchimp.com' filter alone separates marketing-site sessions
    -- from the in-product app on admin.mailchimp.com / login.mailchimp.com.
    visits_periods AS (
      SELECT period, landing_family, channel, geo_country, device, new_returning, COUNT(*) AS visits
      FROM session_dims, UNNEST([
        IF(session_date BETWEEN DATE('{_date(WINDOWS["current_30d"][0])}') AND DATE('{_date(WINDOWS["current_30d"][1])}'), 'current_30d', NULL),
        IF(session_date BETWEEN DATE('{_date(WINDOWS["prior_yr_30d"][0])}') AND DATE('{_date(WINDOWS["prior_yr_30d"][1])}'), 'prior_yr_30d', NULL),
        IF(session_date BETWEEN DATE('{_date(WINDOWS["current_90d"][0])}') AND DATE('{_date(WINDOWS["current_90d"][1])}'), 'current_90d', NULL),
        IF(session_date BETWEEN DATE('{_date(WINDOWS["prior_yr_90d"][0])}') AND DATE('{_date(WINDOWS["prior_yr_90d"][1])}'), 'prior_yr_90d', NULL)
      ]) AS period
      WHERE period IS NOT NULL AND is_acq=1
      GROUP BY 1,2,3,4,5,6
    ),
    signup_users AS (
      SELECT user_pseudo_id, MIN(event_date) AS signup_date
      FROM events WHERE event_name='sign_up'
      GROUP BY 1
    ),
    user_attrs AS (
      SELECT user_pseudo_id,
        ANY_VALUE(landing_family) AS landing_family,
        ANY_VALUE(channel)        AS channel,
        ANY_VALUE(geo_country)    AS geo_country,
        ANY_VALUE(device)         AS device,
        ANY_VALUE(new_returning)  AS new_returning
      FROM session_dims
      GROUP BY 1
    ),
    activations_periods AS (
      SELECT period,
        IFNULL(u.landing_family,'Direct to Signup') AS landing_family,
        IFNULL(u.channel,'Direct')                  AS channel,
        IF(IFNULL(u.geo_country,'Other') IN ({country_list}), IFNULL(u.geo_country,'Other'), 'Other') AS geo_country,
        IFNULL(u.device,'desktop')                  AS device,
        IFNULL(u.new_returning,'new')               AS new_returning,
        COUNT(DISTINCT s.user_pseudo_id) AS activations
      FROM signup_users s
      LEFT JOIN user_attrs u USING(user_pseudo_id),
      UNNEST([
        IF(s.signup_date BETWEEN DATE('{_date(WINDOWS["current_30d"][0])}') AND DATE('{_date(WINDOWS["current_30d"][1])}'), 'current_30d', NULL),
        IF(s.signup_date BETWEEN DATE('{_date(WINDOWS["prior_yr_30d"][0])}') AND DATE('{_date(WINDOWS["prior_yr_30d"][1])}'), 'prior_yr_30d', NULL),
        IF(s.signup_date BETWEEN DATE('{_date(WINDOWS["current_90d"][0])}') AND DATE('{_date(WINDOWS["current_90d"][1])}'), 'current_90d', NULL),
        IF(s.signup_date BETWEEN DATE('{_date(WINDOWS["prior_yr_90d"][0])}') AND DATE('{_date(WINDOWS["prior_yr_90d"][1])}'), 'prior_yr_90d', NULL)
      ]) AS period
      WHERE period IS NOT NULL
      GROUP BY 1,2,3,4,5,6
    )
    SELECT
      COALESCE(v.period, a.period)                 AS period,
      COALESCE(v.landing_family, a.landing_family) AS landing_family,
      COALESCE(v.channel, a.channel)               AS channel,
      COALESCE(v.geo_country, a.geo_country)       AS geo_country,
      COALESCE(v.device, a.device)                 AS device,
      COALESCE(v.new_returning, a.new_returning)   AS new_returning,
      IFNULL(v.visits, 0)      AS visits,
      IFNULL(a.activations, 0) AS activations
    FROM visits_periods v
    FULL OUTER JOIN activations_periods a
      USING(period, landing_family, channel, geo_country, device, new_returning)
    WHERE IFNULL(v.visits,0) >= 5 OR IFNULL(a.activations,0) > 0
    """
    rows = [dict(r) for r in _run(sql)]
    print(f"[ga cube] {len(rows)} rows", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# 2. Cube: BI trials + paid totals per (period, country_group), in one query.
# ---------------------------------------------------------------------------

def build_cube_bi() -> dict[tuple[str, str], dict[str, int]]:
    parts = []
    for period, (s, e) in WINDOWS.items():
        parts.append(f"""
            SELECT '{period}' AS period, country_group,
                   SUM(free_trial_users) AS trials, NULL AS paid
            FROM `mc-business-intelligence.bi_aggregate.free_trials_weekly`
            WHERE week BETWEEN DATE('{_date(s)}') AND DATE('{_date(e)}')
            GROUP BY 1,2
            UNION ALL
            SELECT '{period}' AS period, country_group,
                   NULL AS trials, SUM(total_bookings_users) AS paid
            FROM `mc-business-intelligence.bi_aggregate.bookings_weekly`
            WHERE week BETWEEN DATE('{_date(s)}') AND DATE('{_date(e)}')
            GROUP BY 1,2
        """)
    sql = "SELECT period, country_group, SUM(trials) AS trials, SUM(paid) AS paid FROM (" + " UNION ALL ".join(parts) + ") GROUP BY 1,2"
    out: dict[tuple[str, str], dict[str, int]] = {}
    for r in _run(sql):
        bucket = COUNTRY_GROUP_TO_BUCKET.get(r["country_group"], "Other")
        key = (r["period"], bucket)
        agg = out.setdefault(key, {"trials": 0, "paid": 0})
        agg["trials"] += int(r["trials"] or 0)
        agg["paid"] += int(r["paid"] or 0)
    print(f"[bi cube] {len(out)} (period,country) cells", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# 3. Allocate BI trials/paid across GA dim rows proportional to visits share.
# ---------------------------------------------------------------------------

def merge_cube(ga_rows: list[dict], bi_totals: dict[tuple[str, str], dict[str, int]]) -> list[dict]:
    """Returns the merged cube row list with visits/activations/trials/paid."""
    # Group GA rows by (period, country) to compute visits share.
    by_pc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    sums_by_pc: dict[tuple[str, str], int] = defaultdict(int)
    for r in ga_rows:
        key = (r["period"], r["geo_country"])
        by_pc[key].append(r)
        sums_by_pc[key] += int(r["visits"])

    # For (period,country) cells where GA has 0 visits but BI has trials/paid, we
    # still need a row so the totals show up in the dashboard. Synthesize one
    # "Other / Direct / desktop / new" row per such cell.
    extra_rows = []
    for key, totals in bi_totals.items():
        period, country = key
        if sums_by_pc.get(key, 0) == 0 and (totals["trials"] > 0 or totals["paid"] > 0):
            extra_rows.append({
                "period": period,
                "landing_family": "Other",
                "channel": "Direct",
                "geo_country": country,
                "device": "desktop",
                "new_returning": "new",
                "visits": 0,
                "activations": 0,
            })

    all_rows = ga_rows + extra_rows

    # Recompute groups with extras.
    by_pc.clear()
    sums_by_pc.clear()
    for r in all_rows:
        key = (r["period"], r["geo_country"])
        by_pc[key].append(r)
        sums_by_pc[key] += int(r["visits"])

    # Allocate trials & paid using visits share. If visits==0 in a cell, split
    # evenly across the synthetic rows (only "Other"/"Direct" placeholders).
    for key, rows in by_pc.items():
        totals = bi_totals.get(key, {"trials": 0, "paid": 0})
        if not totals["trials"] and not totals["paid"]:
            for r in rows:
                r.setdefault("trials", 0)
                r.setdefault("paid", 0)
            continue
        total_v = sums_by_pc[key]
        if total_v > 0:
            # Round in a way that preserves the period-country totals exactly.
            for stage in ("trials", "paid"):
                bi_total = int(totals[stage])
                if bi_total <= 0:
                    for r in rows:
                        r.setdefault(stage, 0)
                    continue
                shares = []
                running = 0
                for r in rows:
                    share = int(r["visits"]) * bi_total / total_v
                    floor = int(share)
                    shares.append((r, share - floor))
                    r[stage] = floor
                    running += floor
                # distribute leftover to the largest fractional remainders
                leftover = bi_total - running
                shares.sort(key=lambda t: t[1], reverse=True)
                for i in range(leftover):
                    shares[i % len(shares)][0][stage] += 1
        else:
            # No visits: split evenly across synthetic placeholder rows.
            n = len(rows)
            for stage in ("trials", "paid"):
                bi_total = int(totals[stage])
                base, extra = divmod(bi_total, max(n, 1))
                for i, r in enumerate(rows):
                    r[stage] = base + (1 if i < extra else 0)

    # Final sanity: cap rows-per-period at 30k by dropping smallest by visits+trials+paid.
    capped: list[dict] = []
    by_period: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_period[r["period"]].append(r)
    for period, rows in by_period.items():
        rows.sort(key=lambda r: -(int(r["visits"]) + int(r["trials"]) + int(r["paid"])))
        capped.extend(rows[:30000])
    return capped


# ---------------------------------------------------------------------------
# 4. Trend GA series (visits/activations) by ISO week, plus prior overlay.
# ---------------------------------------------------------------------------

def build_trend_ga(scope_us: bool = False) -> dict[date, dict[str, int]]:
    acq_list = ",".join(f"'{x}'" for x in ACQ_PAGE_GROUPS)
    cur_s = TREND_START_WEEK
    cur_e = TREND_END_WEEK + timedelta(days=6)
    pri_s = cur_s - timedelta(days=TREND_PRIOR_OFFSET_DAYS)
    pri_e = cur_e - timedelta(days=TREND_PRIOR_OFFSET_DAYS)
    us_filter_v = " AND first_page.geo_country='United States'" if scope_us else ""
    us_filter_s = " AND geo_country='United States'" if scope_us else ""
    sql = f"""
    WITH events AS (
      SELECT
        DATE_TRUNC(event_date, WEEK(MONDAY)) AS wk,
        user_pseudo_id, session_id, event_name, event_timestamp,
        user_id, hostname, page_group, page_path, geo_country
      FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
      WHERE (event_date BETWEEN DATE('{_date(pri_s)}') AND DATE('{_date(pri_e)}')
          OR event_date BETWEEN DATE('{_date(cur_s)}') AND DATE('{_date(cur_e)}'))
        AND (hostname='mailchimp.com' OR (event_name='sign_up' AND hostname LIKE '%mailchimp.com'))
    ),
    mc_sessions AS (
      SELECT wk, user_pseudo_id, session_id,
        ARRAY_AGG(
          IF(page_path IS NOT NULL, STRUCT(page_group, geo_country), NULL)
          IGNORE NULLS
          ORDER BY event_timestamp ASC LIMIT 1
        )[SAFE_OFFSET(0)] AS first_page
      FROM events WHERE hostname='mailchimp.com'
      GROUP BY 1,2,3
      HAVING first_page IS NOT NULL
    ),
    visits_wk AS (
      SELECT wk, COUNT(*) AS visits FROM mc_sessions
      WHERE first_page.page_group IN ({acq_list}){us_filter_v}
      GROUP BY 1
    ),
    signup_user_wk AS (
      SELECT wk, user_pseudo_id, ANY_VALUE(geo_country) AS geo_country
      FROM events WHERE event_name='sign_up'
      GROUP BY 1,2
    ),
    activations_wk AS (
      SELECT wk, COUNT(DISTINCT user_pseudo_id) AS activations
      FROM signup_user_wk
      WHERE 1=1{us_filter_s}
      GROUP BY 1
    )
    SELECT COALESCE(v.wk, a.wk) AS wk,
           IFNULL(v.visits,0) AS visits,
           IFNULL(a.activations,0) AS activations
    FROM visits_wk v FULL OUTER JOIN activations_wk a USING(wk)
    ORDER BY wk
    """
    out: dict[date, dict[str, int]] = {}
    for r in _run(sql):
        out[r["wk"]] = {"visits": int(r["visits"] or 0), "activations": int(r["activations"] or 0)}
    return out


# ---------------------------------------------------------------------------
# 5. Trend BI series (trials/paid) by week.
# ---------------------------------------------------------------------------

def build_trend_bi(scope_us: bool = False) -> dict[date, dict[str, int]]:
    # BI weekly aggregates start their week on SUNDAY. The GA-side trend in
    # build_trend_ga starts weeks on MONDAY (DATE_TRUNC ... WEEK(MONDAY)) and
    # build_trend_json's `weeks` list is also Monday-aligned. Without this
    # shift the BI dictionary keys never match the lookup keys and Trial Starts
    # / Paid render as a flat zero line in the trend chart.
    cur_s = TREND_START_WEEK
    cur_e = TREND_END_WEEK + timedelta(days=6)
    pri_s = cur_s - timedelta(days=TREND_PRIOR_OFFSET_DAYS)
    pri_e = cur_e - timedelta(days=TREND_PRIOR_OFFSET_DAYS)
    # Pull the Sunday week that corresponds to each Monday in the trend grid.
    sun_pri_s = pri_s - timedelta(days=1)
    sun_pri_e = pri_e - timedelta(days=1)
    sun_cur_s = cur_s - timedelta(days=1)
    sun_cur_e = cur_e - timedelta(days=1)
    us_filter = " AND country_group='United States'" if scope_us else ""
    sql = f"""
    WITH t AS (
      SELECT DATE_ADD(week, INTERVAL 1 DAY) AS wk, SUM(free_trial_users) AS trials
      FROM `mc-business-intelligence.bi_aggregate.free_trials_weekly`
      WHERE (week BETWEEN DATE('{_date(sun_pri_s)}') AND DATE('{_date(sun_pri_e)}')
          OR week BETWEEN DATE('{_date(sun_cur_s)}') AND DATE('{_date(sun_cur_e)}'))
        {us_filter}
      GROUP BY 1
    ),
    p AS (
      SELECT DATE_ADD(week, INTERVAL 1 DAY) AS wk, SUM(total_bookings_users) AS paid
      FROM `mc-business-intelligence.bi_aggregate.bookings_weekly`
      WHERE (week BETWEEN DATE('{_date(sun_pri_s)}') AND DATE('{_date(sun_pri_e)}')
          OR week BETWEEN DATE('{_date(sun_cur_s)}') AND DATE('{_date(sun_cur_e)}'))
        {us_filter}
      GROUP BY 1
    )
    SELECT COALESCE(t.wk, p.wk) AS wk, IFNULL(t.trials,0) AS trials, IFNULL(p.paid,0) AS paid
    FROM t FULL OUTER JOIN p USING(wk)
    ORDER BY wk
    """
    out: dict[date, dict[str, int]] = {}
    for r in _run(sql):
        out[r["wk"]] = {"trials": int(r["trials"] or 0), "paid": int(r["paid"] or 0)}
    return out


# ---------------------------------------------------------------------------
# 6. Build & write JSON files.
# ---------------------------------------------------------------------------

def build_cube_json() -> tuple[Path, dict]:
    ga_rows = build_cube_ga()
    bi_totals = build_cube_bi()
    rows = merge_cube(ga_rows, bi_totals)
    cube = {
        "as_of": _date(AS_OF),
        "windows": {
            k: {"start": _date(v[0]), "end": _date(v[1])} for k, v in WINDOWS.items()
        },
        "stages": ["visits", "activations", "trials", "paid"],
        "rows": [
            {
                "period": r["period"],
                "landing_family": r["landing_family"],
                "channel": r["channel"],
                "country": r["geo_country"],
                "device": r["device"],
                "new_returning": r["new_returning"],
                "visits": int(r["visits"]),
                "activations": int(r["activations"]),
                "trials": int(r.get("trials", 0)),
                "paid": int(r.get("paid", 0)),
            }
            for r in rows
        ],
    }
    target = DATA_DIR / "funnel-cube.json"
    target.write_text(json.dumps(cube, separators=(",", ":")))
    return target, cube


def build_trend_json() -> tuple[Path, dict]:
    ga = build_trend_ga(scope_us=False)
    bi = build_trend_bi(scope_us=False)
    weeks = [TREND_START_WEEK + timedelta(weeks=i) for i in range(44)]
    weeks_prior = [w - timedelta(days=TREND_PRIOR_OFFSET_DAYS) for w in weeks]

    def pluck(series_ga, series_bi, weeks_list, key):
        out = []
        for w in weeks_list:
            if key in ("visits", "activations"):
                out.append(series_ga.get(w, {}).get(key, 0))
            else:
                out.append(series_bi.get(w, {}).get(key, 0))
        return out

    trend = {
        "weeks": [_date(w) for w in weeks],
        "weeks_prior_year": [_date(w) for w in weeks_prior],
        "stages": ["visits", "activations", "trials", "paid"],
        "series_current": {
            "visits":      pluck(ga, bi, weeks, "visits"),
            "activations": pluck(ga, bi, weeks, "activations"),
            "trials":      pluck(ga, bi, weeks, "trials"),
            "paid":        pluck(ga, bi, weeks, "paid"),
        },
        "series_prior": {
            "visits":      pluck(ga, bi, weeks_prior, "visits"),
            "activations": pluck(ga, bi, weeks_prior, "activations"),
            "trials":      pluck(ga, bi, weeks_prior, "trials"),
            "paid":        pluck(ga, bi, weeks_prior, "paid"),
        },
    }
    target = DATA_DIR / "weekly-trend.json"
    target.write_text(json.dumps(trend, separators=(",", ":")))
    return target, trend


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cube_path, cube = build_cube_json()
    trend_path, trend = build_trend_json()
    print(f"WROTE {cube_path} ({cube_path.stat().st_size:,} bytes, {len(cube['rows'])} rows)")
    print(f"WROTE {trend_path} ({trend_path.stat().st_size:,} bytes, {len(trend['weeks'])} weeks)")
    # FY26 Q1 (US) calibration: align to fiscal weeks 1..13, mirroring the
    # Tableau "FY26 Web Scorecard - Scaled Acquisition" quarter slice.
    bq = _bq()
    fq1_bounds = list(bq.query("""
        SELECT MIN(week) AS first_wk, MAX(week) AS last_wk
        FROM `mc-business-intelligence.bi_aggregate.free_trials_weekly`
        WHERE fy_text='FY26' AND fw_number BETWEEN 1 AND 13
    """).result())[0]
    fq1_start, fq1_end = fq1_bounds["first_wk"], fq1_bounds["last_wk"] + timedelta(days=6)
    us_trials = list(bq.query(f"""
        SELECT SUM(free_trial_users) AS v
        FROM `mc-business-intelligence.bi_aggregate.free_trials_weekly`
        WHERE fy_text='FY26' AND fw_number BETWEEN 1 AND 13 AND country_group='United States'
    """).result())[0]["v"] or 0
    us_paid = list(bq.query(f"""
        SELECT SUM(total_bookings_users) AS v
        FROM `mc-business-intelligence.bi_aggregate.bookings_weekly`
        WHERE fy_text='FY26' AND fw_number BETWEEN 1 AND 13 AND country_group='United States'
    """).result())[0]["v"] or 0
    # GA visits + activations on fw 1..13 calendar window using the SAME
    # methodology the cube + trend now use (no any_uid filter, first event with
    # non-null page_path for entry attributes, ACQ_PAGE_GROUPS carve-out).
    ga_q = list(bq.query(f"""
        WITH ev AS (
          SELECT user_pseudo_id, session_id, event_name, event_timestamp,
                 user_id, hostname, page_group, page_path, geo_country
          FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
          WHERE event_date BETWEEN DATE('{_date(fq1_start)}') AND DATE('{_date(fq1_end)}')
            AND (hostname='mailchimp.com' OR (event_name='sign_up' AND hostname LIKE '%mailchimp.com'))
        ),
        sess AS (
          SELECT user_pseudo_id, session_id,
                 ARRAY_AGG(IF(page_path IS NOT NULL, STRUCT(page_group, geo_country), NULL)
                           IGNORE NULLS ORDER BY event_timestamp ASC LIMIT 1)[SAFE_OFFSET(0)] AS first_page
          FROM ev WHERE hostname='mailchimp.com'
          GROUP BY 1,2
          HAVING first_page IS NOT NULL
        ),
        v AS (
          SELECT COUNT(*) AS visits FROM sess
          WHERE first_page.geo_country='United States'
            AND first_page.page_group IN ({','.join(f"'{x}'" for x in ACQ_PAGE_GROUPS)})
        ),
        s AS (
          SELECT COUNT(DISTINCT user_pseudo_id) AS activations
          FROM (
            SELECT user_pseudo_id, ANY_VALUE(geo_country) AS geo_country
            FROM ev WHERE event_name='sign_up'
            GROUP BY 1
          ) WHERE geo_country='United States'
        )
        SELECT v.visits, s.activations FROM v, s
    """).result())[0]
    us_visits, us_activations = int(ga_q["visits"] or 0), int(ga_q["activations"] or 0)
    ref = {"visits": 7711102, "activations": 131765, "trials": 42284, "paid": 27030}
    print()
    print(f"FY26 Q1 calibration (US scope, fiscal weeks 1-13: {fq1_start}..{fq1_bounds['last_wk']})")
    for stage, measured in [("visits", us_visits), ("activations", us_activations),
                            ("trials", int(us_trials)), ("paid", int(us_paid))]:
        ref_v = ref[stage]
        delta = (measured - ref_v) / ref_v * 100 if ref_v else 0
        flag = "  OK " if abs(delta) <= 10 else " OVER"
        print(f"  {stage:12s}  measured={measured:>12,}  reference={ref_v:>12,}  delta={delta:+6.1f}% {flag}")

    # Sanity print: current 30d / 90d totals that the dashboard will show, for
    # both global and US scope. Helps confirm the new methodology lands in a
    # sensible magnitude (current_90d should be roughly 3x current_30d for a
    # stable funnel) before pushing the rebuilt JSON.
    print()
    print("Dashboard period totals (rebuilt with new methodology)")
    by_period: dict[str, dict[str, int]] = {}
    for p in WINDOWS:
        by_period[p] = {"visits_g": 0, "visits_us": 0, "act_g": 0, "act_us": 0,
                        "tr_g": 0, "tr_us": 0, "pd_g": 0, "pd_us": 0}
    for r in cube["rows"]:
        b = by_period[r["period"]]
        b["visits_g"]   += r["visits"]
        b["act_g"]      += r["activations"]
        b["tr_g"]       += r["trials"]
        b["pd_g"]       += r["paid"]
        if r["country"] == "United States":
            b["visits_us"] += r["visits"]
            b["act_us"]    += r["activations"]
            b["tr_us"]     += r["trials"]
            b["pd_us"]     += r["paid"]
    hdr = f"  {'period':14s} {'visits_g':>12s} {'visits_us':>12s} {'act_g':>9s} {'act_us':>9s} {'tr_g':>9s} {'tr_us':>9s} {'pd_g':>9s} {'pd_us':>9s}"
    print(hdr)
    for p in ("current_30d", "prior_yr_30d", "current_90d", "prior_yr_90d"):
        b = by_period[p]
        print(f"  {p:14s} {b['visits_g']:>12,} {b['visits_us']:>12,} {b['act_g']:>9,} {b['act_us']:>9,} {b['tr_g']:>9,} {b['tr_us']:>9,} {b['pd_g']:>9,} {b['pd_us']:>9,}")


if __name__ == "__main__":
    main()
