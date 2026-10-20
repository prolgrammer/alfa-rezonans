-- Схема 0001: все таблицы спеки docs/specs/2026-10-20-foundation.md, р. 4.
-- Перечисления — CHECK по значениям app/contracts/enums.py (их сверяет tests/db/test_schema.py).
-- Колонки времени — TIMESTAMPTZ (UTC), деньги LLM — NUMERIC(12,4), выручка и платежи — NUMERIC(20,2).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- вход и пользователи

-- пишет: api (orientir seed)
CREATE TABLE users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    -- без FK: учётки создаются до загрузки CSV и без неё (эксперт, администратор, демо без данных)
    company_id TEXT,
    user_role TEXT NOT NULL CHECK (user_role IN ('owner', 'ceo', 'cfo', 'treasurer', 'chief_accountant', 'other')),
    access TEXT NOT NULL CHECK (access IN ('client', 'expert', 'admin')),
    interests JSONB NOT NULL DEFAULT '[]',
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX users_company_id_idx ON users (company_id);

-- пишет: api
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    acting_company_id TEXT,
    acting_user_role TEXT CHECK (
        acting_user_role IN ('owner', 'ceo', 'cfo', 'treasurer', 'chief_accountant', 'other')
    ),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_id_idx ON sessions (user_id);
CREATE INDEX sessions_expires_at_idx ON sessions (expires_at);

-- пишет: api
CREATE TABLE audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT REFERENCES users (id),
    action TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_user_id_idx ON audit_log (user_id);
CREATE INDEX audit_log_created_at_idx ON audit_log (created_at);

-- ---------------------------------------------------------------- клиенты (CSV организаторов)

-- пишет: роль 2 (orientir load-clients)
-- Колонки CSV — только названные в общем ТЗ (A1, A2); остальное лежит в raw до получения данных.
CREATE TABLE companies (
    id TEXT PRIMARY KEY,
    mainokved_code TEXT,
    revenue_company NUMERIC(20, 2),
    group_id TEXT,
    region TEXT,
    city TEXT,
    cl_type TEXT,
    okfs_name TEXT,
    org_type TEXT,
    company_age_years_gradation TEXT,
    cl_product_rko_mark BOOLEAN,
    cl_product_gar_mark BOOLEAN,
    cl_product_loa_mark BOOLEAN,
    cl_product_lea_mark BOOLEAN,
    cl_product_dep_mark BOOLEAN,
    cl_product_zpp_mark BOOLEAN,
    factoring_mark BOOLEAN,
    fts_flag BOOLEAN,
    fts_country_name TEXT,
    negativemark TEXT,
    eco_mark_label TEXT,
    raw JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX companies_group_id_idx ON companies (group_id);
CREATE INDEX companies_mainokved_code_idx ON companies (mainokved_code);

-- пишет: роль 2 (orientir load-clients)
CREATE TABLE company_persons (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    person_id TEXT NOT NULL,
    rc_name TEXT,
    raw JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX company_persons_company_id_idx ON company_persons (company_id);

-- пишет: роль 2 (orientir load-clients)
CREATE TABLE payments (
    -- без FK: платежи на неизвестные ID остаются в таблице, их показывает отчёт A1
    payer_id TEXT NOT NULL,
    beneficiary_id TEXT NOT NULL,
    pay_amt NUMERIC(20, 2) NOT NULL,
    pay_cnt INTEGER NOT NULL,
    intragroup BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (payer_id, beneficiary_id)
);
CREATE INDEX payments_beneficiary_id_idx ON payments (beneficiary_id);

-- пишет: роль 2 (worker)
CREATE TABLE company_profiles (
    company_id TEXT PRIMARY KEY REFERENCES companies (id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    hash TEXT NOT NULL,
    completeness DOUBLE PRECISION NOT NULL CHECK (completeness BETWEEN 0 AND 1),
    features JSONB NOT NULL,
    exposures JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- пишет: роль 2 (worker)
CREATE TABLE group_profiles (
    group_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    hash TEXT NOT NULL,
    completeness DOUBLE PRECISION NOT NULL CHECK (completeness BETWEEN 0 AND 1),
    features JSONB NOT NULL,
    exposures JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- пишет: api
CREATE TABLE profile_overrides (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id TEXT NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users (id),
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('edit', 'text')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX profile_overrides_company_id_idx ON profile_overrides (company_id);

-- ---------------------------------------------------------------- новости и события

-- пишет: worker (orientir seed загружает config/sources.yaml)
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('rss', 'html', 'api')),
    kind TEXT NOT NULL CHECK (kind IN ('official', 'news', 'industry')),
    url TEXT NOT NULL,
    trust DOUBLE PRECISION NOT NULL CHECK (trust BETWEEN 0 AND 1),
    lang TEXT NOT NULL DEFAULT 'ru',
    interval_min INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    disabled_reason TEXT,
    config JSONB NOT NULL DEFAULT '{}'
);

-- пишет: worker
CREATE TABLE raw_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources (id),
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'filtered_out', 'clustered')),
    filter_reason TEXT,
    factors TEXT[] NOT NULL DEFAULT '{}',
    sectors TEXT[] NOT NULL DEFAULT '{}',
    regions TEXT[] NOT NULL DEFAULT '{}',
    tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(content, ''))
    ) STORED
);
CREATE INDEX raw_items_tsv_idx ON raw_items USING GIN (tsv);
CREATE INDEX raw_items_factors_idx ON raw_items USING GIN (factors);
CREATE INDEX raw_items_sectors_idx ON raw_items USING GIN (sectors);
CREATE INDEX raw_items_published_at_idx ON raw_items (published_at);
CREATE INDEX raw_items_source_id_idx ON raw_items (source_id);
CREATE INDEX raw_items_status_idx ON raw_items (status);
CREATE INDEX raw_items_content_hash_idx ON raw_items (content_hash);

-- пишет: worker; card* — функция роли 1
CREATE TABLE events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title TEXT NOT NULL,
    primary_item_id BIGINT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_item_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_count INTEGER NOT NULL DEFAULT 1 CHECK (source_count >= 1),
    has_official BOOLEAN NOT NULL DEFAULT false,
    factors TEXT[] NOT NULL DEFAULT '{}',
    sectors TEXT[] NOT NULL DEFAULT '{}',
    regions TEXT[] NOT NULL DEFAULT '{}',
    card JSONB,
    card_status TEXT NOT NULL DEFAULT 'none' CHECK (card_status IN ('none', 'pending', 'ok', 'failed')),
    card_key TEXT,
    conflicts JSONB NOT NULL DEFAULT '[]',
    embedding VECTOR(384)
);
CREATE INDEX events_factors_idx ON events USING GIN (factors);
CREATE INDEX events_sectors_idx ON events USING GIN (sectors);
CREATE INDEX events_last_item_at_idx ON events (last_item_at);
CREATE INDEX events_card_status_idx ON events (card_status);

-- пишет: worker
CREATE TABLE event_items (
    event_id BIGINT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    raw_item_id BIGINT NOT NULL REFERENCES raw_items (id),
    is_primary BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (event_id, raw_item_id)
);
CREATE INDEX event_items_raw_item_id_idx ON event_items (raw_item_id);

-- пишет: worker
CREATE TABLE market_series (
    series TEXT NOT NULL,
    date DATE NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (series, date)
);

-- ---------------------------------------------------------------- аналитика банка

-- пишет: orientir ingest-bank
CREATE TABLE bank_docs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title TEXT NOT NULL,
    published_on DATE NOT NULL,
    topic TEXT,
    file_sha256 TEXT NOT NULL UNIQUE,
    pages INTEGER NOT NULL CHECK (pages >= 1),
    factors TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX bank_docs_factors_idx ON bank_docs USING GIN (factors);

-- пишет: orientir ingest-bank
CREATE TABLE bank_chunks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES bank_docs (id) ON DELETE CASCADE,
    page INTEGER NOT NULL CHECK (page >= 1),
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('russian', coalesce(text, ''))) STORED,
    embedding VECTOR(384),
    chart_png TEXT
);
CREATE INDEX bank_chunks_tsv_idx ON bank_chunks USING GIN (tsv);
CREATE INDEX bank_chunks_doc_id_idx ON bank_chunks (doc_id, page, ord);

-- пишет: роль 1 (extract_theses)
CREATE TABLE bank_theses (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES bank_docs (id) ON DELETE CASCADE,
    page INTEGER NOT NULL CHECK (page >= 1),
    payload JSONB NOT NULL,
    factors TEXT[] NOT NULL DEFAULT '{}',
    kind TEXT NOT NULL CHECK (kind IN ('forecast', 'assessment', 'fact', 'recommendation')),
    published_on DATE NOT NULL,
    verified BOOLEAN,
    prompt_version TEXT NOT NULL
);
CREATE INDEX bank_theses_doc_id_idx ON bank_theses (doc_id);
CREATE INDEX bank_theses_factors_idx ON bank_theses USING GIN (factors);
CREATE INDEX bank_theses_published_on_idx ON bank_theses (published_on);

-- ---------------------------------------------------------------- отбор, разборы, агент

-- пишет: роль 2
CREATE TABLE relevance (
    company_id TEXT NOT NULL,
    user_role TEXT NOT NULL CHECK (user_role IN ('owner', 'ceo', 'cfo', 'treasurer', 'chief_accountant', 'other')),
    event_id BIGINT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    score DOUBLE PRECISION NOT NULL,
    terms JSONB NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('shown', 'below_threshold', 'duplicate', 'excluded', 'diversity_cut')),
    reason TEXT,
    judge DOUBLE PRECISION CHECK (judge BETWEEN 0 AND 3),
    profile_hash TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, user_role, event_id)
);
CREATE INDEX relevance_event_id_idx ON relevance (event_id);

-- пишет: роль 1 (через обёртку api)
CREATE TABLE insights (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events (id),
    -- без FK: разбор можно запросить для компании, которой нет в загруженных CSV (демо без данных)
    company_id TEXT NOT NULL,
    user_role TEXT NOT NULL CHECK (user_role IN ('owner', 'ceo', 'cfo', 'treasurer', 'chief_accountant', 'other')),
    cache_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed')),
    mode TEXT CHECK (mode IN ('full', 'brief')),
    payload JSONB,
    cost_rub NUMERIC(12, 4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX insights_event_id_idx ON insights (event_id);
CREATE INDEX insights_company_id_idx ON insights (company_id);
CREATE INDEX insights_status_idx ON insights (status);

-- пишет: роль 1
CREATE TABLE agent_traces (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    insight_id BIGINT REFERENCES insights (id) ON DELETE CASCADE,
    chat_id UUID,
    seq INTEGER NOT NULL,
    step JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (insight_id IS NOT NULL OR chat_id IS NOT NULL)
);
CREATE INDEX agent_traces_insight_idx ON agent_traces (insight_id, seq);
CREATE INDEX agent_traces_chat_idx ON agent_traces (chat_id, seq);

-- пишет: роль 1
CREATE TABLE tool_calls (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    insight_id BIGINT,
    chat_id UUID,
    user_id BIGINT NOT NULL,
    tool TEXT NOT NULL,
    args JSONB NOT NULL,
    result_chars INTEGER NOT NULL CHECK (result_chars >= 0),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    ok BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (insight_id IS NOT NULL OR chat_id IS NOT NULL)
);
CREATE INDEX tool_calls_insight_id_idx ON tool_calls (insight_id);
CREATE INDEX tool_calls_chat_id_idx ON tool_calls (chat_id);

-- пишет: шлюз роли 1
CREATE TABLE llm_calls (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_rub NUMERIC(12, 4) NOT NULL DEFAULT 0,
    ms INTEGER NOT NULL DEFAULT 0,
    cached BOOLEAN NOT NULL DEFAULT false,
    fallback_from TEXT,
    error TEXT,
    lane TEXT NOT NULL,
    trace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- по этой колонке считается суточный расход
CREATE INDEX llm_calls_created_at_idx ON llm_calls (created_at);

-- пишет: шлюз роли 1
CREATE TABLE llm_cache (
    key TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- отзывы и чат

-- пишет: api
CREATE TABLE feedback (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    event_id BIGINT NOT NULL REFERENCES events (id),
    insight_id BIGINT,
    verdict TEXT NOT NULL CHECK (verdict IN ('useful', 'miss')),
    reason TEXT CHECK (reason IN ('not_my_topic', 'already_knew', 'factual_error', 'wrong_conclusion')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX feedback_user_id_idx ON feedback (user_id);
CREATE INDEX feedback_event_id_idx ON feedback (event_id);

-- пишет: api
CREATE TABLE expert_reviews (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    insight_id BIGINT NOT NULL REFERENCES insights (id),
    reviewer_id BIGINT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('accept', 'edit', 'reject')),
    patch JSONB NOT NULL DEFAULT '{}',
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX expert_reviews_insight_id_idx ON expert_reviews (insight_id);

-- пишет: api
CREATE TABLE chat_messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id UUID NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users (id),
    event_id BIGINT,
    author TEXT NOT NULL CHECK (author IN ('user', 'assistant')),
    content TEXT NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX chat_messages_chat_id_idx ON chat_messages (chat_id, id);
CREATE INDEX chat_messages_user_id_idx ON chat_messages (user_id);
