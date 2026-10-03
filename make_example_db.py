"""Create an example 'shop' database at schema version 1."""
import sqlite3
from pathlib import Path

db = Path(__file__).parent / "data" / "shop.db"
db.parent.mkdir(exist_ok=True)
if db.exists():
    db.unlink()
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys = ON")
conn.executescript("""
CREATE TABLE _schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    sha256      TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
INSERT INTO products (name) VALUES ('apple'), ('pear');
""")
conn.commit()
conn.close()
print(f"created {db}")
