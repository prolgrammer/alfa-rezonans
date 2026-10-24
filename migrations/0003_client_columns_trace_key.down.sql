ALTER TABLE agent_traces DROP CONSTRAINT agent_traces_insight_id_seq_key;
CREATE INDEX agent_traces_insight_idx ON agent_traces (insight_id, seq);

ALTER TABLE company_persons DROP COLUMN city_name;
ALTER TABLE companies
    DROP COLUMN amount_workers_spark_gradation,
    DROP COLUMN amount_workers_fns_gradation,
    DROP COLUMN mainokved_name;
