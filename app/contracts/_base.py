from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    # Поля со значениями по умолчанию в ответе присутствуют всегда: в схеме ответа они обязательны,
    # иначе сгенерированные типы фронта сделают их необязательными.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)
