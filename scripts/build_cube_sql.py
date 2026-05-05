"""Generate the consolidated cube SQL string for all 4 windows.

This mirrors what `build_data.build_cube_ga()` actually runs in BigQuery; it
exists so the SQL can be diffed / shared without running the full Python build.
"""

ACQ = ("homepage","marketing_pricing_page","solutions","solutions_pages_email_marketing",
       "solutions_pages_sms_marketing","solutions_pages_marketing_automation",
       "solutions_pages_templates","signup_start_page","other_switch_to_mailchimp",
       "overview_pages","sales","contact_pages","landing_pages")

TOP_COUNTRIES = [
  "United States","Canada","United Kingdom","India","Mexico","China","Spain","Australia",
  "Germany","Japan","Argentina","Saudi Arabia","Ireland","Brazil","France","Italy",
  "Netherlands","Singapore","Thailand","Malaysia"
]

# Window definitions: as_of = 2026-05-02 (last day of last complete BI week,
# week of Sun 2026-04-26 .. Sat 2026-05-02; today is 2026-05-04 Mon).
WINDOWS = {
  "current_30d":  ("2026-04-03", "2026-05-02"),
  "prior_yr_30d": ("2025-04-03", "2025-05-02"),
  "current_90d":  ("2026-02-02", "2026-05-02"),
  "prior_yr_90d": ("2025-02-02", "2025-05-02"),
}

acq_list = ",".join([f"'{x}'" for x in ACQ])
country_list = ",".join([f"'{c}'" for c in TOP_COUNTRIES])

# Union of all needed event_date ranges:
#   2025-02-02..2025-05-02  (covers prior_yr_90d which contains prior_yr_30d)
#   2026-02-02..2026-05-02  (covers current_90d which contains current_30d)
sql = f"""
WITH events AS (
  SELECT
    user_pseudo_id, session_id, event_name, event_timestamp, event_date,
    user_id, hostname, page_group, page_path, geo_country, device_category,
    ga_session_number, traffic_source_source, traffic_source_medium
  FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
  WHERE (event_date BETWEEN DATE('2025-02-02') AND DATE('2025-05-02')
      OR event_date BETWEEN DATE('2026-02-02') AND DATE('2026-05-02'))
    AND (hostname='mailchimp.com' OR (event_name='sign_up' AND hostname LIKE '%mailchimp.com'))
),
mc_sessions AS (
  -- Entry attributes are taken from the FIRST event with a non-null page_path.
  -- In 2026 GA4 emits many ambient/automation events with NULL page_path; the
  -- prior logic of ORDER BY event_timestamp ASC LIMIT 1 (no NULL filter) often
  -- picked one of those, causing the carve-out below to drop the session.
  SELECT
    user_pseudo_id, session_id,
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
      ELSE 'desktop' END AS device,
    CASE
      WHEN LOWER(first_page.traffic_source_medium) IN ('cpc','ppc','paidsearch','paid-search','paid_search') THEN 'Paid Search'
      WHEN LOWER(first_page.traffic_source_medium) IN ('paid_social','paidsocial','paid-social','social-paid','social_paid','cpm') THEN 'Paid Social'
      WHEN LOWER(first_page.traffic_source_medium) IN ('organic','seo') THEN 'Organic Search'
      WHEN LOWER(first_page.traffic_source_medium) IN ('social','social-organic','social_organic','organic_social','organic-social') THEN 'Organic Social'
      WHEN LOWER(first_page.traffic_source_medium) IN ('email','newsletter') THEN 'Email'
      WHEN LOWER(first_page.traffic_source_medium) IN ('affiliate','affiliates') THEN 'Affiliate'
      WHEN LOWER(first_page.traffic_source_medium) = 'referral' THEN 'Referral'
      WHEN LOWER(first_page.traffic_source_medium) IN ('display','banner','cpv') THEN 'Display / Other'
      WHEN LOWER(first_page.traffic_source_medium) IN ('','(none)','none','direct') OR first_page.traffic_source_medium IS NULL OR LOWER(first_page.traffic_source_source) IN ('(direct)','direct') THEN 'Direct'
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
visits_periods AS (
  -- Visits = all sessions whose entry page is in the acquisition carve-out.
  -- We deliberately do NOT filter on user_id IS NULL: in 2026 GA4 began
  -- persisting user_id across sessions for any visitor who once authenticated
  -- on the device, so any_uid=0 now excludes 40-60% of legitimate prospect
  -- traffic on mailchimp.com (was 10-15% in FY26 Q1 2025).
  SELECT period, landing_family, channel, geo_country, device, new_returning, COUNT(*) AS visits
  FROM session_dims, UNNEST([
    IF(session_date BETWEEN DATE('2026-04-03') AND DATE('2026-05-02'), 'current_30d', NULL),
    IF(session_date BETWEEN DATE('2025-04-03') AND DATE('2025-05-02'), 'prior_yr_30d', NULL),
    IF(session_date BETWEEN DATE('2026-02-02') AND DATE('2026-05-02'), 'current_90d', NULL),
    IF(session_date BETWEEN DATE('2025-02-02') AND DATE('2025-05-02'), 'prior_yr_90d', NULL)
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
activations_periods AS (
  SELECT
    period,
    IFNULL(u.landing_family,'Direct to Signup') AS landing_family,
    IFNULL(u.channel,'Direct')                  AS channel,
    IF(IFNULL(u.geo_country,'Other') IN ({country_list}), IFNULL(u.geo_country,'Other'), 'Other') AS geo_country,
    IFNULL(u.device,'desktop')                  AS device,
    IFNULL(u.new_returning,'new')               AS new_returning,
    COUNT(DISTINCT s.user_pseudo_id) AS activations
  FROM signup_users s
  LEFT JOIN user_attrs u USING(user_pseudo_id),
  UNNEST([
    IF(s.signup_date BETWEEN DATE('2026-04-03') AND DATE('2026-05-02'), 'current_30d', NULL),
    IF(s.signup_date BETWEEN DATE('2025-04-03') AND DATE('2025-05-02'), 'prior_yr_30d', NULL),
    IF(s.signup_date BETWEEN DATE('2026-02-02') AND DATE('2026-05-02'), 'current_90d', NULL),
    IF(s.signup_date BETWEEN DATE('2025-02-02') AND DATE('2025-05-02'), 'prior_yr_90d', NULL)
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
FULL OUTER JOIN activations_periods a USING(period, landing_family, channel, geo_country, device, new_returning)
WHERE IFNULL(v.visits,0) >= 5 OR IFNULL(a.activations,0) > 0
"""
print(sql)
