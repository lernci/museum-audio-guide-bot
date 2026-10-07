-- Museum Audio Guide Bot — schema
-- Written for PostgreSQL. For SQLite: drop the CHECK-based enums stay as-is
-- (portable), replace SERIAL with INTEGER PRIMARY KEY AUTOINCREMENT, and
-- TIMESTAMPTZ with TEXT (store ISO8601) or plain TIMESTAMP.

-- ─────────────────────────────────────────────────────────────────────────
-- staff_users: allowlist for the Admin FSM side of the bot
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE staff_users (
    telegram_user_id   BIGINT PRIMARY KEY,
    full_name           TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'content'
                          CHECK (role IN ('content', 'owner')),
    added_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────
-- exhibits: one row per physical exhibit / plaque / QR code
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE exhibits (
    id                   TEXT PRIMARY KEY,        -- human exhibit number, e.g. "007"
    title_am             TEXT NOT NULL,            -- staff-entered title (Armenian)
    fact_sheet_am        TEXT NOT NULL,            -- staff-entered raw facts (Armenian)
    qr_code_path         TEXT,                     -- filesystem path to generated QR PNG
    deep_link            TEXT,                     -- t.me/<bot>?start=exh_<id>
    status               TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'processing', 'review', 'live', 'unpublished', 'failed')),
    created_by           BIGINT REFERENCES staff_users(telegram_user_id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────
-- exhibit_photos: 1-3 photos per exhibit, ordered. We store the Telegram
-- file_id only — Telegram hosts the actual image, so we can re-send it for
-- free by id instead of keeping files on our own disk.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE exhibit_photos (
    id                   SERIAL PRIMARY KEY,
    exhibit_id           TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    telegram_file_id     TEXT NOT NULL,
    sort_order           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (exhibit_id, sort_order)
);

CREATE INDEX idx_exhibit_photos_exhibit ON exhibit_photos(exhibit_id);

-- ─────────────────────────────────────────────────────────────────────────
-- audio_cache: one row per (exhibit, language) — the pre-generated asset
-- This is the table the visitor-facing bot reads from for 0-second delivery.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE audio_cache (
    id                   SERIAL PRIMARY KEY,
    exhibit_id           TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    language_code        TEXT NOT NULL
                          CHECK (language_code IN
                            ('am','en','ru','fr','es','de','fa','zh','it','el')),
    script_text          TEXT,                     -- narration script actually spoken
    title_translated     TEXT,                     -- exhibit title localized to this language
    tts_provider         TEXT
                          CHECK (tts_provider IN ('local_am', 'openai', 'elevenlabs')),
    voice_id             TEXT,                     -- provider-specific voice/model id
    telegram_file_id     TEXT,                     -- voice file_id from the log channel
    telegram_file_unique_id TEXT,
    duration_seconds     REAL,
    status               TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'generating', 'ready', 'failed')),
    error_message        TEXT,
    generated_at         TIMESTAMPTZ,
    UNIQUE (exhibit_id, language_code)
);

CREATE INDEX idx_audio_cache_exhibit ON audio_cache(exhibit_id);
CREATE INDEX idx_audio_cache_status  ON audio_cache(status);

-- ─────────────────────────────────────────────────────────────────────────
-- generation_jobs: durable queue / progress+retry tracking for the pipeline.
-- The in-process asyncio worker is fed from an in-memory queue, but this
-- table is the source of truth so a bot restart can requeue unfinished work.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE generation_jobs (
    id                   SERIAL PRIMARY KEY,
    exhibit_id           TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    job_type             TEXT NOT NULL
                          CHECK (job_type IN ('qr', 'translate', 'tts')),
    language_code        TEXT,                     -- NULL for job_type='qr'
    status               TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'success', 'failed')),
    attempts             INTEGER NOT NULL DEFAULT 0,
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_generation_jobs_status ON generation_jobs(status);
CREATE INDEX idx_generation_jobs_exhibit ON generation_jobs(exhibit_id);

-- ─────────────────────────────────────────────────────────────────────────
-- exhibit_views: optional lightweight analytics — which exhibits/languages
-- are actually being used, useful to show the client usage data later.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE exhibit_views (
    id                   SERIAL PRIMARY KEY,
    exhibit_id           TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
    telegram_user_id     BIGINT NOT NULL,
    language_code        TEXT NOT NULL,
    viewed_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_exhibit_views_exhibit ON exhibit_views(exhibit_id);
