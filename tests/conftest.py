import os

# Настройки обязательны для get_settings(); тестам хватает заглушек, в сеть и БД они не ходят.
os.environ.setdefault("DATABASE_URL", "postgresql://orientir:orientir@localhost:5432/orientir")
os.environ.setdefault("DEMO_PASSWORD", "test-password")
