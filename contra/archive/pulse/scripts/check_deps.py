import duckdb
from pathlib import Path

con = duckdb.connect(str(Path(__file__).parent.parent / "pulse.duckdb"))

print("=== All tables ===")
for t in con.execute("SELECT table_name FROM duckdb_tables()").fetchall():
    print(" ", t[0])

print("\n=== All views ===")
for v in con.execute("SELECT view_name FROM duckdb_views()").fetchall():
    print(" ", v[0])

print("\n=== All constraints referencing entities_raw ===")
try:
    rows = con.execute("""
        SELECT constraint_column_names, constraint_type, table_name
        FROM duckdb_constraints()
        WHERE table_name = 'entities_raw'
    """).fetchall()
    for r in rows:
        print(" ", r)
except Exception as e:
    print(" Error:", e)

print("\n=== Tables with FK to entities_raw ===")
try:
    rows = con.execute("""
        SELECT fk_table: table_name, c.column_name, c.column_type
        FROM duckdb_columns() c
        WHERE c.column_name = 'source_record_id'
    """).fetchall()
    for r in rows:
        print(" ", r)
except Exception as e:
    print(" FK query error:", e)

# Try simpler: list all sequences and macros
print("\n=== Sequences ===")
try:
    for s in con.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall():
        print(" ", s[0])
except Exception as e:
    print(" Error:", e)

print("\n=== Indexes ===")
try:
    for i in con.execute("SELECT index_name, table_name FROM duckdb_indexes()").fetchall():
        print(" ", i[0], "->", i[1])
except Exception as e:
    print(" Error:", e)

con.close()
