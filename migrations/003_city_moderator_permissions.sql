-- H1 RADAR: city-scoped moderator permissions.
-- Applied automatically by the app schema as well; safe to run repeatedly.
CREATE TABLE IF NOT EXISTS moderator_city_permissions (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    city_id BIGINT NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK (permission IN (
        'events','marks','people','beta','outages','statistics',
        'appeals','announcements','feed','support','ambassador'
    )),
    granted_by BIGINT NOT NULL REFERENCES users(telegram_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_id, city_id, permission)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_city_ambassador_idx
    ON moderator_city_permissions(city_id) WHERE permission='ambassador';
CREATE INDEX IF NOT EXISTS moderator_city_permissions_user_idx
    ON moderator_city_permissions(telegram_id, city_id);
