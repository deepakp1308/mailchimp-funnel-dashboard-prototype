"""Generate the consolidated cube SQL string for all 4 windows."""
import sys

ACQ = ("homepage","marketing_pricing_page","solutions","solutions_pages_email_marketing",
       "solutions_pages_sms_marketing","solutions_pages_marketing_automation",
       "solutions_pages_templates","signup_start_page","other_switch_to_mailchimp",
       "overview_pages","sales","contact_pages")

TOP_COUNTRIES = [
  "United States","Canada","United Kingdom","India","Mexico","China","Spain","Australia",
  "Germany","Japan","Argentina","Saudi Arabia","Ireland","Brazil","France","Italy",
  "Netherlands","Singapore","Thailand","Malaysia"
]

# Window definitions: as_of = 2026-05-03 (yesterday from 2026-05-04)
WINDOWS = {
  "current_30d": ("2026-04-04", "2026-05-03"),
  "prior_yr_30d": ("2025-04-04", "2025-05-03"),
  "current_90d": ("2026-02-03", "2026-05-03"),
  "prior_yr_90d": ("2025-02-03", "2025-05-03"),
}

acq_list = ",".join([f"'{x}'" for x in ACQ])
country_list = ",".join([f"'{c}'" for c in TOP_COUNTRIES])

# Union of all needed event_date ranges:
# 2025-02-03..2025-05-03  (covers prior_yr_90d which contains prior_yr_30d)
# 2026-02-03..2026-05-03  (covers current_90d which contains current_30d)
sql = f"""
WITH events AS (
  SELECT
    user_pseudo_id, session_id, event_name, event_timestamp, event_date,
    user_id, hostname, page_group, geo_country, device_category,
    ga_session_number, traffic_source_source, traffic_source_medium
  FROM `mc-business-intelligence.google_analytics.fct_ga4_visitor_session_events_daily`
  WHERE (event_date BETWEEN DATE('2025-02-03') AND DATE('2025-05-03')
      OR event_date BETWEEN DATE('2026-02-03') AND DATE('2026-05-03'))
    AND (hostname='mailchimp.com' OR (event_name='sign_up' AND hostname LIKE '%mailchimp.com'))
),
mc_sessions AS (
  SELECT
    user_pseudo_id, session_id,
    MIN(event_date) AS session_date,
    MAX(IF(user_id IS NOT NULL,1,0)) AS any_uid,
    MAX(IF(ga_session_number=1,1,0)) AS is_new,
    ARRAY_AGG(STRUCT(page_group, traffic_source_source, traffic_source_medium, geo_country, device_category)
              ORDER BY event_timestamp ASC LIMIT 1)[OFFSET(0)] AS entry
  FROM events WHERE hostname='mailchimp.com'
  GROUP BY 1,2
),
session_dims AS (
  SELECT
    user_pseudo_id, session_id, session_date, any_uid,
    IF(is_new=1,'new','returning') AS new_returning,
    IF(IFNULL(entry.geo_country,'Other') IN ({country_list}), entry.geo_country, 'Other') AS geo_country,
    CASE LOWER(IFNULL(entry.device_category,''))
      WHEN 'mobile' THEN 'mobile' WHEN 'desktop' THEN 'desktop' WHEN 'tablet' THEN 'tablet'
      ELSE 'desktop' END AS device,
    CASE
      WHEN LOWER(entry.traffic_source_medium) IN ('cpc','ppc','paidsearch','paid-search','paid_search') THEN 'Paid Search'
      WHEN LOWER(entry.traffic_source_medium) IN ('paid_social','paidsocial','paid-social','social-paid','social_paid','cpm') THEN 'Paid Social'
      WHEN LOWER(entry.traffic_source_medium) IN ('organic','seo') THEN 'Organic Search'
      WHEN LOWER(entry.traffic_source_medium) IN ('social','social-organic','social_organic','organic_social','organic-social') THEN 'Organic Social'
      WHEN LOWER(entry.traffic_source_medium) IN ('email','newsletter') THEN 'Email'
      WHEN LOWER(entry.traffic_source_medium) IN ('affiliate','affiliates') THEN 'Affiliate'
      WHEN LOWER(entry.traffic_source_medium) = 'referral' THEN 'Referral'
      WHEN LOWER(entry.traffic_source_medium) IN ('display','banner','cpv') THEN 'Display / Other'
      WHEN LOWER(entry.traffic_source_medium) IN ('','(none)','none','direct') OR entry.traffic_source_medium IS NULL OR LOWER(entry.traffic_source_source) IN ('(direct)','direct') THEN 'Direct'
      ELSE 'Display / Other'
    END AS channel,
    CASE
      WHEN entry.page_group='homepage' THEN 'Homepage'
      WHEN entry.page_group='marketing_pricing_page' THEN 'Pricing'
      WHEN entry.page_group='solutions_pages_email_marketing' THEN 'Email Marketing solution'
      WHEN entry.page_group='solutions_pages_sms_marketing' THEN 'SMS Marketing solution'
      WHEN entry.page_group='solutions_pages_marketing_automation' THEN 'Marketing Automation solution'
      WHEN entry.page_group IN ('solutions','solutions_pages_templates','overview_pages','sales','contact_pages','expert_directory','onboarding_services','personalize') THEN 'Other Solutions'
      WHEN entry.page_group='feature_pages' OR entry.page_group LIKE 'features_pages_%' THEN 'Other Feature pages'
      WHEN entry.page_group IN ('resources_pages','resources_pages_email','resources_pages_benchmarks','resources_pages_home','resources_pages_deliverability','marketing_glossary_pages','mailchimp_presents','mailchimp_story_pages') THEN 'SEO Resources'
      WHEN entry.page_group='knowledge_base_pages' OR entry.page_group LIKE 'help_pages_%' THEN 'Knowledge Base / Help'
      WHEN entry.page_group LIKE 'integrations_pages%' THEN 'Integrations'
      WHEN entry.page_group='other_switch_to_mailchimp' THEN 'Switch-to / Compete'
      WHEN entry.page_group='signup_start_page' THEN 'Direct to Signup'
      ELSE 'Other'
    END AS landing_family,
    IF(entry.page_group IN ({acq_list}),1,0) AS is_acq
  FROM mc_sessions
),
visits_periods AS (
  SELECT period, landing_family, channel, geo_country, device, new_returning, COUNT(*) AS visits
  FROM session_dims, UNNEST([
    IF(session_date BETWEEN DATE('2026-04-04') AND DATE('2026-05-03'), 'current_30d', NULL),
    IF(session_date BETWEEN DATE('2025-04-04') AND DATE('2025-05-03'), 'prior_yr_30d', NULL),
    IF(session_date BETWEEN DATE('2026-02-03') AND DATE('2026-05-03'), 'current_90d', NULL),
    IF(session_date BETWEEN DATE('2025-02-03') AND DATE('2025-05-03'), 'prior_yr_90d', NULL)
  ]) AS period
  WHERE period IS NOT NULL AND any_uid=0 AND is_acq=1
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
    IF(s.signup_date BETWEEN DATE('2026-04-04') AND DATE('2026-05-03'), 'current_30d', NULL),
    IF(s.signup_date BETWEEN DATE('2025-04-04') AND DATE('2025-05-03'), 'prior_yr_30d', NULL),
    IF(s.signup_date BETWEEN DATE('2026-02-03') AND DATE('2026-05-03'), 'current_90d', NULL),
    IF(s.signup_date BETWEEN DATE('2025-02-03') AND DATE('2025-05-03'), 'prior_yr_90d', NULL)
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
