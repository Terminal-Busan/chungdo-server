CREATE TABLE IF NOT EXISTS policies (
  id VARCHAR(64) PRIMARY KEY,
  name VARCHAR(300) NOT NULL,
  field VARCHAR(50), agency VARCHAR(200), agency_type VARCHAR(50),
  region VARCHAR(100), purpose_summary TEXT, target_text TEXT,
  criteria_text TEXT, benefit_text TEXT, apply_method TEXT,
  apply_deadline TEXT, reception_agency TEXT, contact TEXT,
  documents TEXT, online_apply_url TEXT, detail_url TEXT,
  conditions JSONB NOT NULL DEFAULT '{}', gov24_updated_at TEXT,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_demo BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS navigations (
  id UUID PRIMARY KEY,
  applied_policy_id VARCHAR(64) NOT NULL REFERENCES policies(id),
  rejection_text TEXT, profile JSONB NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'done', 'failed')),
  result JSONB,
  started_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '24 hours'
);
CREATE TABLE IF NOT EXISTS agent_steps (
  id BIGSERIAL PRIMARY KEY,
  navigation_id UUID NOT NULL REFERENCES navigations(id) ON DELETE CASCADE,
  agent_name VARCHAR(50) NOT NULL,
  step_type VARCHAR(20) NOT NULL,
  content JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_policies_field ON policies(field);
CREATE INDEX IF NOT EXISTS idx_policies_region ON policies(region);
CREATE INDEX IF NOT EXISTS idx_agent_steps_nav ON agent_steps(navigation_id, id);
CREATE INDEX IF NOT EXISTS idx_navigations_expiry ON navigations(expires_at);
-- Compatible with existing tables created from the original TRD.
ALTER TABLE policies ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE navigations ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
