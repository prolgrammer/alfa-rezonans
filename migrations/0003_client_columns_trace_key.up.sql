-- Поля CSV организаторов, на которые опираются признаки профиля (A2): отрасль словами, градации
-- работников по ФНС и СПАРК, город физлица. Значения градаций заранее не известны, поэтому TEXT,
-- как у company_age_years_gradation.
ALTER TABLE companies
    ADD COLUMN mainokved_name TEXT,
    ADD COLUMN amount_workers_fns_gradation TEXT,
    ADD COLUMN amount_workers_spark_gradation TEXT;
ALTER TABLE company_persons ADD COLUMN city_name TEXT;

-- Шаг разбора хранится одной строкой: повтор записи после обрыва связи перезаписывает её.
-- Дубли, накопленные до ключа, схлопываются в последнюю строку — её же брало чтение.
-- Строк чата ключ не касается: insight_id у них пуст.
DELETE FROM agent_traces older
USING agent_traces newer
WHERE older.insight_id = newer.insight_id AND older.seq = newer.seq AND older.id < newer.id;
DROP INDEX agent_traces_insight_idx;
ALTER TABLE agent_traces ADD CONSTRAINT agent_traces_insight_id_seq_key UNIQUE (insight_id, seq);
