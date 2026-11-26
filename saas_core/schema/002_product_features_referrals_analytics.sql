-- Tloomberg SaaS product entitlements and privacy extensions.
-- Apply after 001_initial.sql to the dedicated SaaS PostgreSQL database only.
BEGIN;

CREATE TABLE IF NOT EXISTS product_features (
  code VARCHAR(80) PRIMARY KEY,
  title VARCHAR(120) NOT NULL,
  description TEXT NOT NULL,
  category VARCHAR(40) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO product_features(code,title,description,category) VALUES
 ('market.overview','نمای کلی بازار','نمای کلی و اطلاعات پایهٔ بازار','market'),
 ('stock.basic','صفحهٔ پایهٔ نماد','اطلاعات پایهٔ نمادها','market'),
 ('shareholders.graphs','نمودارهای سهام‌داری','نمودارهای مالکیت و ترکیب سهام‌داران','analysis'),
 ('shareholders.deep_analysis','تحلیل عمیق سهام‌داران','تاریخچه و تحلیل تکمیلی سهام‌داری','analysis'),
 ('screener.advanced','فیلتر پیشرفته','فیلتر و رتبه‌بندی پیشرفته','analysis'),
 ('portfolio.reconstruction','بازسازی پرتفوی','بازسازی و ارزش‌گذاری پرتفوی','portfolio'),
 ('investor.history','تاریخچهٔ سرمایه‌گذار','روند ارزش پرتفوی و گرایش تاریخی','portfolio'),
 ('exports.download','خروجی داده','خروجی‌های مجاز Excel و CSV','exports')
ON CONFLICT(code) DO NOTHING;

INSERT INTO plans(code,title,duration_days,price_toman,active)
VALUES ('free','حساب رایگان',36500,0,TRUE)
ON CONFLICT(code) DO NOTHING;

CREATE TABLE IF NOT EXISTS plan_features (
  plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
  feature_code VARCHAR(80) NOT NULL REFERENCES product_features(code) ON DELETE RESTRICT,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY(plan_id,feature_code)
);

-- Free plan has only basic market access. Further restrictions can be added later.
INSERT INTO plan_features(plan_id,feature_code,enabled)
SELECT p.id,f.code,TRUE
FROM plans p CROSS JOIN product_features f
WHERE p.code='free' AND f.code IN ('market.overview','stock.basic')
ON CONFLICT(plan_id,feature_code) DO UPDATE SET enabled=EXCLUDED.enabled;

-- Paid plans unlock all customer-facing features. Market-data administration
-- is a staff permission and can never be acquired by buying a subscription.
INSERT INTO plan_features(plan_id,feature_code,enabled)
SELECT p.id,f.code,TRUE
FROM plans p CROSS JOIN product_features f
WHERE p.code IN ('premium_monthly','premium_quarterly')
ON CONFLICT(plan_id,feature_code) DO UPDATE SET enabled=EXCLUDED.enabled;

INSERT INTO roles(code,title) VALUES ('data_manager','Market Data Manager')
ON CONFLICT(code) DO NOTHING;
INSERT INTO role_permissions(role_id,permission_id)
SELECT r.id,p.id
FROM roles r JOIN permissions p ON p.code='market.admin'
WHERE r.code='data_manager'
ON CONFLICT DO NOTHING;

-- An invite only qualifies after a new account verifies the invited phone.
CREATE TABLE IF NOT EXISTS referral_invitations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inviter_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  referral_code VARCHAR(24) NOT NULL REFERENCES referral_codes(code) ON DELETE RESTRICT,
  invited_phone_e164 VARCHAR(20) NOT NULL,
  invited_user_id UUID UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  status VARCHAR(16) NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','verified','rewarded','rejected','expired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  phone_verified_at TIMESTAMPTZ,
  rewarded_at TIMESTAMPTZ,
  CHECK(invited_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
  CHECK(inviter_user_id IS DISTINCT FROM invited_user_id),
  UNIQUE(invited_phone_e164)
);
CREATE INDEX IF NOT EXISTS idx_referral_invites_inviter_time
  ON referral_invitations(inviter_user_id,created_at DESC);

-- Consent changes are auditable for authenticated and anonymous visitors.
CREATE TABLE IF NOT EXISTS analytics_consent_history (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  anonymous_id UUID,
  product_analytics BOOLEAN NOT NULL,
  consent_version VARCHAR(24) NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK(user_id IS NOT NULL OR anonymous_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_analytics_consent_user_time
  ON analytics_consent_history(user_id,changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_consent_anon_time
  ON analytics_consent_history(anonymous_id,changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_events_path_component_time
  ON analytics_events(page_path,component_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_events_anon_session_time
  ON analytics_events(anonymous_id,session_id,occurred_at DESC);

-- Resolve actual customer features on the server for every protected request.
CREATE OR REPLACE VIEW effective_user_features AS
SELECT u.id AS user_id, pf.feature_code
FROM users u
LEFT JOIN LATERAL (
  SELECT s.plan_id
  FROM subscriptions s
  WHERE s.user_id=u.id
    AND s.status='active'
    AND (s.starts_at IS NULL OR s.starts_at<=now())
    AND (s.ends_at IS NULL OR s.ends_at>now())
  ORDER BY s.ends_at DESC NULLS LAST, s.created_at DESC
  LIMIT 1
) active_subscription ON TRUE
JOIN plans effective_plan
  ON effective_plan.id=COALESCE(
    active_subscription.plan_id,
    (SELECT free_plan.id FROM plans free_plan WHERE free_plan.code='free' LIMIT 1)
  )
JOIN plan_features pf ON pf.plan_id=effective_plan.id AND pf.enabled=TRUE;

COMMIT;
