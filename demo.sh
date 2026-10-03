#!/bin/bash
# Demo: upgrade the example 'shop' database and show failure rollback.
set -u
BASE=http://localhost:8141
cd "$(dirname "$0")"

echo '== current version =='
curl -s $BASE/db/shop/version; echo

python3 - <<'PY'
import json
m1 = """CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), note TEXT);
INSERT INTO customers (name) VALUES ('ada; lovelace');
-- a comment; with a semicolon
CREATE TRIGGER trg_order AFTER INSERT ON orders BEGIN
  UPDATE customers SET name = name WHERE id = NEW.customer_id;
  INSERT INTO orders (note, customer_id) VALUES ('audit; row', NEW.customer_id);
END;"""
m2 = "INSERT INTO orders (customer_id, note) VALUES (1, 'first; order'); /* block; comment */"
json.dump({"expected_version": 2, "migrations": [
    {"version": 1, "description": "orders tables + trigger", "sql": m1},
    {"version": 2, "description": "seed order", "sql": m2}]},
    open("/tmp/mig_ok.json", "w"))
bad = json.load(open("/tmp/mig_ok.json"))
bad["expected_version"] = 3
bad["migrations"].append({"version": 3, "description": "broken",
                          "sql": "CREATE TABLE t3(x); INSERT INTO nope VALUES (1);"})
json.dump(bad, open("/tmp/mig_bad.json", "w"))
PY

echo '== apply v1..v2 =='
curl -s -X POST $BASE/db/shop/migrate -H 'Content-Type: application/json' -d @/tmp/mig_ok.json; echo
echo '== resubmit same manifest (no re-execution) =='
curl -s -X POST $BASE/db/shop/migrate -H 'Content-Type: application/json' -d @/tmp/mig_ok.json; echo
echo '== failing v3: whole batch rolls back =='
curl -s -X POST $BASE/db/shop/migrate -H 'Content-Type: application/json' -d @/tmp/mig_bad.json; echo
echo '== final state (still v2) =='
curl -s $BASE/db/shop/version; echo
