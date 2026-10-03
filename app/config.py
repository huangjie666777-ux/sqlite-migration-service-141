"""Alias -> database file mapping. Clients only ever see aliases."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

ALIASES = {
    "shop": DATA_DIR / "shop.db",
    "blog": DATA_DIR / "blog.db",
}

MAX_REQUEST_BYTES = 1024 * 1024  # 1 MiB
MAX_MIGRATIONS_PER_REQUEST = 200

MIGRATION_TABLE = "_schema_migrations"
