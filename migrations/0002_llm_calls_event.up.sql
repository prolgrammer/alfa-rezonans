-- Стоимость события и смета суток по статьям (ML-3) считаются по llm_calls, поэтому вызов
-- запоминает событие, ради которого он сделан. Колонка необязательна: не у всех вызовов есть событие,
-- а удаление или склейка событий не должны упираться в историю вызовов.
ALTER TABLE llm_calls ADD COLUMN event_id BIGINT REFERENCES events (id) ON DELETE SET NULL;
CREATE INDEX llm_calls_event_id_idx ON llm_calls (event_id);
