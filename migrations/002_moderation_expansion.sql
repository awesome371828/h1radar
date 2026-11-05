-- H1 RADAR moderation expansion
-- Safe to apply repeatedly on PostgreSQL 15+.
CREATE TABLE IF NOT EXISTS user_restrictions (
 telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
 reason TEXT NOT NULL DEFAULT '',
 expires_at TIMESTAMPTZ,
 created_by BIGINT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_restrictions_expiry_idx ON user_restrictions(expires_at);

CREATE TABLE IF NOT EXISTS city_requests (
 id BIGSERIAL PRIMARY KEY,
 telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 city_name TEXT NOT NULL,
 region TEXT NOT NULL DEFAULT '',
 latitude DOUBLE PRECISION,
 longitude DOUBLE PRECISION,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 moderator_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS city_requests_status_idx ON city_requests(status,created_at DESC);
