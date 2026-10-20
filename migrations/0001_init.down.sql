-- Расширение vector не удаляем: его могут использовать другие схемы этой базы,
-- а DROP EXTENSION требует прав владельца.

DROP TABLE chat_messages;
DROP TABLE expert_reviews;
DROP TABLE feedback;
DROP TABLE llm_cache;
DROP TABLE llm_calls;
DROP TABLE tool_calls;
DROP TABLE agent_traces;
DROP TABLE insights;
DROP TABLE relevance;
DROP TABLE bank_theses;
DROP TABLE bank_chunks;
DROP TABLE bank_docs;
DROP TABLE market_series;
DROP TABLE event_items;
DROP TABLE events;
DROP TABLE raw_items;
DROP TABLE sources;
DROP TABLE profile_overrides;
DROP TABLE group_profiles;
DROP TABLE company_profiles;
DROP TABLE payments;
DROP TABLE company_persons;
DROP TABLE companies;
DROP TABLE audit_log;
DROP TABLE sessions;
DROP TABLE users;
