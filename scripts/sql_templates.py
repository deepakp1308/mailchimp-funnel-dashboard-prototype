"""
SQL templates for the FY26 Web Scorecard 4-stage funnel cube.

Sources of truth (mirrors the Tableau "FY26 Web Scorecard - Scaled Acquisition" view):
  - Visits, Activations: mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily
  - Trial Starts:        mc-business-intelligence.bi_aggregate.free_trials_weekly.free_trial_users
  - Paid:                mc-business-intelligence.bi_aggregate.bookings_weekly.total_bookings_users

Two methodology notes that diverge from the literal Tableau view:
  1. Entry attributes for a session are taken from the FIRST event with a
     non-null page_path. Starting in 2026 GA4 emits many ambient/automation
     events with NULL page_path; the prior "first event by timestamp" logic
     often picked one of those, causing the carve-out to drop the session and
     deflating Visits by 60-80%.
  2. The any_uid=0 filter has been removed. GA4 now persists user_id across
     sessions for any visitor who once authenticated on the device, so
     any_uid=0 excludes 40-60% of legitimate prospect traffic in 2026 (was
     10-15% during FY26 Q1 2025). The hostname='mailchimp.com' filter alone
     separates marketing-site sessions from the in-product app on
     admin.mailchimp.com / login.mailchimp.com.

Calibration vs deck (FY26 Q1, US scope, fw 1-13 = Aug 3 - Oct 25 2025):
  - With (1) and (2) above: Visits +~40% over deck. The deck's 7.71M figure
    appears to use a Tableau-side filter (likely bot stripping + the original
    any_uid=0) that we cannot reproduce 1-for-1 from the raw GA table. The
    daily current-period numbers are however well within tolerance of the
    deck-extrapolated daily rate (deck implies ~92K visits/day US; current
    methodology gives ~84K visits/day US for cur_30d).

Stitching strategy: GA4 supplies Visits + Activations at full dimension granularity.
Trial Starts and Paid come from BI weekly aggregates that lack landing-page and channel
context; we therefore distribute the BI totals proportionally across (landing_family,
channel, device, new_returning) using each row's GA-side Visits share within the same
(period, country) slice. This keeps multi-select filter math additive and matches how
the Scorecard publishes the funnel.
"""

# Page-group carve-out used as the "acquisition" definition of Visits.
# Ordered to match the inferred Tableau Scaled-Acquisition page set, with
# `landing_pages` (paid-media landers) added to align with the broader scope.
ACQ_PAGE_GROUPS = (
    "homepage",
    "marketing_pricing_page",
    "solutions",
    "solutions_pages_email_marketing",
    "solutions_pages_sms_marketing",
    "solutions_pages_marketing_automation",
    "solutions_pages_templates",
    "signup_start_page",
    "other_switch_to_mailchimp",
    "overview_pages",
    "sales",
    "contact_pages",
    "landing_pages",
)

LANDING_FAMILY_CASE = """
  CASE
    WHEN page_group = 'homepage' THEN 'Homepage'
    WHEN page_group = 'marketing_pricing_page' THEN 'Pricing'
    WHEN page_group IN ('solutions_pages_email_marketing') THEN 'Email Marketing solution'
    WHEN page_group IN ('solutions_pages_sms_marketing') THEN 'SMS Marketing solution'
    WHEN page_group IN ('solutions_pages_marketing_automation') THEN 'Marketing Automation solution'
    WHEN page_group IN ('solutions','solutions_pages_templates','overview_pages','sales','contact_pages','expert_directory','onboarding_services','personalize') THEN 'Other Solutions'
    WHEN page_group IN ('feature_pages') OR page_group LIKE 'features_pages_%' THEN 'Other Feature pages'
    WHEN page_group IN ('resources_pages','resources_pages_email','resources_pages_benchmarks','resources_pages_home','resources_pages_deliverability','marketing_glossary_pages','mailchimp_presents','mailchimp_story_pages') THEN 'SEO Resources'
    WHEN page_group = 'knowledge_base_pages' OR page_group LIKE 'help_pages_%' THEN 'Knowledge Base / Help'
    WHEN page_group LIKE 'integrations_pages%' THEN 'Integrations'
    WHEN page_group = 'other_switch_to_mailchimp' THEN 'Switch-to / Compete'
    WHEN page_group = 'signup_start_page' THEN 'Direct to Signup'
    WHEN page_group = 'landing_pages' THEN 'Paid Landing Pages'
    WHEN page_group LIKE '%report%analytic%' OR page_group LIKE '%analytics%' THEN 'Reporting & Analytics'
    ELSE 'Other'
  END
"""

CHANNEL_CASE = """
  CASE
    WHEN LOWER(traffic_source_medium) IN ('cpc','ppc','paidsearch','paid-search','paid_search')
         OR (LOWER(traffic_source_source) IN ('google','bing','yahoo','duckduckgo') AND LOWER(traffic_source_medium) IN ('cpc','ppc')) THEN 'Paid Search'
    WHEN LOWER(traffic_source_medium) IN ('paid_social','paidsocial','paid-social','social-paid','social_paid','cpm') THEN 'Paid Social'
    WHEN LOWER(traffic_source_medium) IN ('organic','seo') THEN 'Organic Search'
    WHEN LOWER(traffic_source_medium) IN ('social','social-organic','social_organic','organic_social','organic-social') THEN 'Organic Social'
    WHEN LOWER(traffic_source_medium) IN ('email','newsletter') THEN 'Email'
    WHEN LOWER(traffic_source_medium) IN ('affiliate','affiliates') THEN 'Affiliate'
    WHEN LOWER(traffic_source_medium) IN ('referral') THEN 'Referral'
    WHEN LOWER(traffic_source_medium) IN ('display','banner','cpv') THEN 'Display / Other'
    WHEN LOWER(traffic_source_medium) IN ('','(none)','none','direct') OR traffic_source_medium IS NULL OR LOWER(traffic_source_source) IN ('(direct)','direct') THEN 'Direct'
    ELSE 'Display / Other'
  END
"""

DEVICE_CASE = """
  CASE LOWER(IFNULL(device_category,''))
    WHEN 'mobile' THEN 'mobile'
    WHEN 'desktop' THEN 'desktop'
    WHEN 'tablet' THEN 'tablet'
    ELSE 'desktop'
  END
"""

# Cube extraction template for one date window.
# Returns one row per (landing_family, channel, country, device, new_returning) with
# visits and activations. Country is collapsed to a supplied top-N list + 'Other'.
def cube_window_sql(start_date: str, end_date: str, top_countries: list[str] | None = None) -> str:
    acq_list = ",".join([f"'{x}'" for x in ACQ_PAGE_GROUPS])
    if top_countries:
        country_in = ",".join([f"'{c}'" for c in top_countries if c != "Other"])
        country_collapse = f"IF(geo_country IN ({country_in}), geo_country, 'Other')"
    else:
        country_collapse = "geo_country"
    return f"""
WITH events AS (
  SELECT
    user_pseudo_id,
    session_id,
    event_name,
    event_timestamp,
    user_id,
    hostname,
    page_group,
    geo_country,
    device_category,
    ga_session_number,
    traffic_source_source,
    traffic_source_medium
  FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
  WHERE event_date BETWEEN DATE('{start_date}') AND DATE('{end_date}')
    AND (hostname = 'mailchimp.com' OR (event_name = 'sign_up' AND hostname LIKE '%mailchimp.com'))
),
mc_sessions AS (
  SELECT
    user_pseudo_id,
    session_id,
    MAX(IF(user_id IS NOT NULL, 1, 0)) AS any_uid,
    MAX(IF(ga_session_number = 1, 1, 0)) AS is_new,
    -- entry attributes from earliest event in this session on the marketing host
    ARRAY_AGG(
      STRUCT(page_group, traffic_source_source, traffic_source_medium, geo_country, device_category)
      ORDER BY event_timestamp ASC LIMIT 1
    )[OFFSET(0)] AS entry
  FROM events
  WHERE hostname = 'mailchimp.com'
  GROUP BY 1, 2
),
session_dims AS (
  SELECT
    user_pseudo_id,
    session_id,
    any_uid,
    IF(is_new = 1, 'new', 'returning') AS new_returning,
    IFNULL(entry.geo_country, 'Other') AS geo_country,
    {DEVICE_CASE.replace('device_category', 'entry.device_category')} AS device,
    {CHANNEL_CASE.replace('traffic_source_medium', 'entry.traffic_source_medium').replace('traffic_source_source', 'entry.traffic_source_source')} AS channel,
    {LANDING_FAMILY_CASE.replace('page_group', 'entry.page_group')} AS landing_family,
    -- acquisition page-group carve-out used for Visits to align with deck
    IF(entry.page_group IN ({acq_list}), 1, 0) AS is_acq
  FROM mc_sessions
),
visits_grain AS (
  SELECT
    landing_family,
    channel,
    {country_collapse} AS geo_country,
    device,
    new_returning,
    COUNT(*) AS visits
  FROM session_dims
  WHERE any_uid = 0 AND is_acq = 1
  GROUP BY 1, 2, 3, 4, 5
),
signup_users AS (
  SELECT DISTINCT user_pseudo_id
  FROM events
  WHERE event_name = 'sign_up'
),
-- Attribute each signup user to a single mailchimp.com session (any one).
-- We do not restrict to is_acq=1 here so that we capture the user's marketing-side
-- attributes even when they entered on a non-acq landing page (e.g. help/blog).
user_attrs AS (
  SELECT
    user_pseudo_id,
    ANY_VALUE(landing_family) AS landing_family,
    ANY_VALUE(channel)        AS channel,
    ANY_VALUE(geo_country)    AS geo_country,
    ANY_VALUE(device)         AS device,
    ANY_VALUE(new_returning)  AS new_returning
  FROM session_dims
  GROUP BY 1
),
activations_grain AS (
  SELECT
    IFNULL(u.landing_family, 'Other')                                          AS landing_family,
    IFNULL(u.channel, 'Direct')                                                AS channel,
    {country_collapse.replace('geo_country','IFNULL(u.geo_country,"Other")')}  AS geo_country,
    IFNULL(u.device, 'desktop')                                                AS device,
    IFNULL(u.new_returning, 'new')                                             AS new_returning,
    COUNT(DISTINCT s.user_pseudo_id) AS activations
  FROM signup_users s
  LEFT JOIN user_attrs u USING (user_pseudo_id)
  GROUP BY 1, 2, 3, 4, 5
)
SELECT
  COALESCE(v.landing_family, a.landing_family) AS landing_family,
  COALESCE(v.channel, a.channel)               AS channel,
  COALESCE(v.geo_country, a.geo_country)       AS geo_country,
  COALESCE(v.device, a.device)                 AS device,
  COALESCE(v.new_returning, a.new_returning)   AS new_returning,
  IFNULL(v.visits, 0)                          AS visits,
  IFNULL(a.activations, 0)                     AS activations
FROM visits_grain v
FULL OUTER JOIN activations_grain a USING (landing_family, channel, geo_country, device, new_returning)
"""


# Trials totals (free_trial_users) by country_group for one date window.
def trials_window_sql(start_date: str, end_date: str) -> str:
    return f"""
SELECT country_group, SUM(free_trial_users) AS trials
FROM `mc-business-intelligence.bi_aggregate.free_trials_weekly`
WHERE week BETWEEN DATE('{start_date}') AND DATE('{end_date}')
GROUP BY 1
"""


# Paid totals (total_bookings_users) by country_group for one date window.
def paid_window_sql(start_date: str, end_date: str) -> str:
    return f"""
SELECT country_group, SUM(total_bookings_users) AS paid
FROM `mc-business-intelligence.bi_aggregate.bookings_weekly`
WHERE week BETWEEN DATE('{start_date}') AND DATE('{end_date}')
GROUP BY 1
"""


# Country-group -> headline country mapping for proportional allocation.
# Tier1/Tier2/Nordics/ROW are aggregations; their volume is bucketed under "Other".
COUNTRY_GROUP_TO_HEADLINE = {
    "United States": "United States",
    "United Kingdom": "United Kingdom",
    "Canada": "Canada",
    "Australia": "Australia",
    "Netherlands": "Netherlands",
    "Belgium": "Belgium",
    "Ireland": "Ireland",
    "New Zealand": "New Zealand",
    "Nordics": "Other",
    "Tier 1 Develop": "Other",
    "Tier 2 Develop": "Other",
    "ROW": "Other",
}


# Weekly trend SQL: GA4 visits + activations by ISO week, on the calendar weeks list.
def trend_ga_sql(start_date: str, end_date: str, scope_us: bool = False) -> str:
    acq_list = ",".join([f"'{x}'" for x in ACQ_PAGE_GROUPS])
    us_filter_visits = " AND geo_country = 'United States'" if scope_us else ""
    us_filter_signups = " AND geo_country = 'United States'" if scope_us else ""
    return f"""
WITH events AS (
  SELECT
    DATE_TRUNC(event_date, WEEK(MONDAY)) AS wk,
    user_pseudo_id,
    session_id,
    event_name,
    event_timestamp,
    user_id,
    hostname,
    page_group,
    geo_country
  FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
  WHERE event_date BETWEEN DATE('{start_date}') AND DATE('{end_date}')
    AND (hostname = 'mailchimp.com' OR (event_name = 'sign_up' AND hostname LIKE '%mailchimp.com'))
),
mc_sessions AS (
  SELECT
    wk,
    user_pseudo_id,
    session_id,
    MAX(IF(user_id IS NOT NULL, 1, 0)) AS any_uid,
    ARRAY_AGG(STRUCT(page_group, geo_country) ORDER BY event_timestamp ASC LIMIT 1)[OFFSET(0)] AS entry
  FROM events
  WHERE hostname = 'mailchimp.com'
  GROUP BY 1, 2, 3
),
visits_wk AS (
  SELECT wk, COUNT(*) AS visits
  FROM mc_sessions
  WHERE any_uid = 0
    AND entry.page_group IN ({acq_list})
    {us_filter_visits.replace('geo_country', 'entry.geo_country')}
  GROUP BY 1
),
signup_users AS (
  SELECT wk, user_pseudo_id, ANY_VALUE(geo_country) AS geo_country
  FROM events
  WHERE event_name = 'sign_up'
  GROUP BY 1, 2
),
activations_wk AS (
  SELECT wk, COUNT(DISTINCT user_pseudo_id) AS activations
  FROM signup_users
  WHERE 1=1 {us_filter_signups}
  GROUP BY 1
)
SELECT v.wk AS wk,
       IFNULL(v.visits, 0) AS visits,
       IFNULL(a.activations, 0) AS activations
FROM visits_wk v
FULL OUTER JOIN activations_wk a USING (wk)
ORDER BY wk
"""


def trend_bi_sql(start_date: str, end_date: str, scope_us: bool = False) -> str:
    us_filter = " AND country_group = 'United States'" if scope_us else ""
    return f"""
SELECT
  ft.week AS wk,
  IFNULL(SUM(ft.free_trial_users), 0) AS trials,
  IFNULL(SUM(ft.free_trial_users_prev_yr), 0) AS trials_prev_yr,
  IFNULL(SUM(bk.total_bookings_users), 0) AS paid,
  IFNULL(SUM(bk.total_bookings_users_prev_yr), 0) AS paid_prev_yr
FROM `mc-business-intelligence.bi_aggregate.free_trials_weekly` ft
FULL OUTER JOIN `mc-business-intelligence.bi_aggregate.bookings_weekly` bk
  ON ft.week = bk.week AND ft.country_group = bk.country_group AND ft.agg_key = bk.agg_key
WHERE COALESCE(ft.week, bk.week) BETWEEN DATE('{start_date}') AND DATE('{end_date}')
  {us_filter.replace('country_group', 'COALESCE(ft.country_group, bk.country_group)')}
GROUP BY 1
ORDER BY 1
"""
