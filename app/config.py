"""服务配置（通过环境变量覆盖）。"""

import os

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg2://cleanroom:cleanroom@localhost:5432/cleanroom"
)


class Settings:
    def __init__(self):
        self.database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


settings = Settings()
