CREATE TABLE IF NOT EXISTS users (
 telegram_id BIGINT PRIMARY KEY, username TEXT, display_name TEXT NOT NULL,
 phone TEXT NOT NULL, public_code TEXT UNIQUE NOT NULL,
 role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','moderator','owner')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reports (
 id BIGSERIAL PRIMARY KEY, author_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 city TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('alert','all_clear','shelter','info')),
 description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','published','rejected')),
 moderator_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reports_feed_idx ON reports(status,created_at DESC);
CREATE TABLE IF NOT EXISTS moderation_log (
 id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL, action TEXT NOT NULL,
 target_id BIGINT, details TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cities (
 id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, region TEXT NOT NULL DEFAULT '', latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, enabled BOOLEAN NOT NULL DEFAULT true
);
INSERT INTO cities(name,region,latitude,longitude) VALUES
('Москва','Москва',55.7558,37.6173),
('Санкт-Петербург','Санкт-Петербург',59.9343,30.3351),
('Ростов-на-Дону','Ростовская область',47.2357,39.7015),
('Батайск','Ростовская область',47.14,39.75),
('Краснодар','Краснодарский край',45.0355,38.9753),
('Сочи','Краснодарский край',43.5855,39.7231),
('Волгоград','Волгоградская область',48.708,44.5133),
('Воронеж','Воронежская область',51.6608,39.2003),
('Казань','Республика Татарстан',55.7961,49.1064),
('Нижний Новгород','Нижегородская область',56.2965,43.9361),
('Самара','Самарская область',53.1959,50.1002),
('Саратов','Саратовская область',51.5336,46.0343),
('Уфа','Республика Башкортостан',54.7388,55.9721),
('Пермь','Пермский край',58.0105,56.2502),
('Екатеринбург','Свердловская область',56.8389,60.6057),
('Челябинск','Челябинская область',55.1644,61.4368),
('Тюмень','Тюменская область',57.1522,65.5272),
('Омск','Омская область',54.9885,73.3242),
('Новосибирск','Новосибирская область',55.0084,82.9357),
('Томск','Томская область',56.501,84.9924),
('Кемерово','Кемеровская область',55.3547,86.0873),
('Красноярск','Красноярский край',56.0153,92.8932),
('Иркутск','Иркутская область',52.2864,104.2807),
('Улан-Удэ','Республика Бурятия',51.8348,107.5845),
('Чита','Забайкальский край',52.034,113.4994),
('Якутск','Республика Саха',62.0355,129.6755),
('Хабаровск','Хабаровский край',48.4802,135.0719),
('Владивосток','Приморский край',43.1155,131.8855),
('Южно-Сахалинск','Сахалинская область',46.9591,142.738),
('Петропавловск-Камчатский','Камчатский край',53.037,158.6559),
('Мурманск','Мурманская область',68.9707,33.0749),
('Архангельск','Архангельская область',64.5393,40.5187),
('Калининград','Калининградская область',54.7104,20.4522),
('Курск','Курская область',51.7304,36.1926),
('Белгород','Белгородская область',50.5957,36.5872),
('Липецк','Липецкая область',52.6088,39.5992),
('Тула','Тульская область',54.1931,37.6173),
('Ярославль','Ярославская область',57.6261,39.8845),
('Ижевск','Удмуртская Республика',56.8527,53.2115),
('Оренбург','Оренбургская область',51.7682,55.0969)
ON CONFLICT(name) DO UPDATE SET region=EXCLUDED.region,latitude=EXCLUDED.latitude,longitude=EXCLUDED.longitude;


-- H1 RADAR nearby marks: coarse coordinates and independent confirmations.
ALTER TABLE reports ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS confirmations INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_category_check;
ALTER TABLE reports ADD CONSTRAINT reports_category_check
 CHECK(category IN ('alert','all_clear','shelter','info','drone','explosion','siren','rocket'));
CREATE TABLE IF NOT EXISTS report_confirmations (
 report_id BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
 telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(report_id,telegram_id)
);
CREATE TABLE IF NOT EXISTS login_challenges (
 nonce TEXT PRIMARY KEY,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL,
 telegram_id BIGINT,
 consumed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS web_sessions (
 token_hash TEXT PRIMARY KEY,
 telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS city_chat_messages (
 id BIGSERIAL PRIMARY KEY,
 city TEXT NOT NULL,
 telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 username TEXT,
 display_name TEXT NOT NULL,
 message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 1000),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS city_chat_city_time_idx ON city_chat_messages(city,created_at DESC);
