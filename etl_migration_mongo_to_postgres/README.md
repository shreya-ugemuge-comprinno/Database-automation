# etl_migration_mongo_to_postgres — MongoDB → PostgreSQL Migration Tool

A lightweight two-file Python CLI that automates database migration from
MongoDB to PostgreSQL, following a 7-phase pipeline.

---

## Files

```
etl_migration_mongo_to_postgres/
├── migrate_mongo_to_postgres.py  ← main script (all 7 phases)
├── type_map.py                   ← MongoDB BSON → PostgreSQL type mapping
├── requirements.txt              ← Python dependencies
└── README.md                     ← this file
```

---

## Prerequisites

### 1. Python 3.10+
### 2. Python dependencies
```cmd
pip install -r requirements.txt
```
### 3. MongoDB Server (source)
### 4. PostgreSQL Server 13+ (target)

---

## Quick Start

### Local MongoDB (no auth)
```cmd
python migrate_mongo_to_postgres.py ^
  --src-host localhost --src-db myapp ^
  --tgt-host localhost --tgt-db myapp_pg ^
  --tgt-user postgres --tgt-pass YourPassword ^
  --out ./migration_output --drop-target
```

### With MongoDB Authentication
```cmd
python migrate_mongo_to_postgres.py ^
  --src-host localhost --src-db myapp ^
  --src-user mongoUser --src-pass mongoPass --src-auth-db admin ^
  --tgt-host localhost --tgt-db myapp_pg ^
  --tgt-user postgres --tgt-pass YourPassword ^
  --out ./migration_output --drop-target
```

> **Always use `--drop-target`** when re-running to avoid duplicate key errors.

---

## All CLI Arguments

### Source (MongoDB)
| Argument | Default | Description |
|---|---|---|
| `--src-host` | `localhost` | MongoDB hostname |
| `--src-port` | `27017` | MongoDB port |
| `--src-db` | *(required)* | Source database name |
| `--src-user` | None | MongoDB username (omit for no auth) |
| `--src-pass` | None | MongoDB password |
| `--src-auth-db` | `admin` | MongoDB auth database |

### Target (PostgreSQL)
| Argument | Default | Description |
|---|---|---|
| `--tgt-host` | `localhost` | PostgreSQL hostname |
| `--tgt-port` | `5432` | PostgreSQL port |
| `--tgt-db` | *(required)* | Target database name |
| `--tgt-user` | *(required)* | PostgreSQL username |
| `--tgt-pass` | *(required)* | PostgreSQL password |

### General
| Argument | Default | Description |
|---|---|---|
| `--sample-size` | `0` (full scan) | Docs to sample for schema inference |
| `--out` | `./migration_mongo_to_postgres_output` | Output directory |
| `--drop-target` | false | Drop and recreate target DB |
| `--audit-only` | false | Schema discovery only, no migration |

---

## 7-Phase Pipeline

| Phase | What happens |
|---|---|
| **1–2. Discovery** | Scans all collections, infers field types from documents. Flattens nested sub-documents. Generates `audit.json`. |
| **3. Create tables** | Creates PostgreSQL tables with inferred column types. `_id` field becomes PRIMARY KEY. |
| **4. Migrate data** | Reads documents, converts BSON values to PG-compatible types, inserts in batches of 1000. Validates row counts. |
| **5. Indexes** | Recreates MongoDB indexes in PostgreSQL. |
| **6–7. Validation** | Row count comparison per table. Writes `migration_report.json`. |

---

## Data Type Mapping

| MongoDB/BSON Type | PostgreSQL Type | Notes |
|---|---|---|
| `ObjectId` | `CHAR(24)` | Hex string, PRIMARY KEY |
| `string` | `TEXT` | |
| `int` (32-bit) | `INTEGER` | |
| `long` (64-bit) | `BIGINT` | |
| `double` | `DOUBLE PRECISION` | |
| `Decimal128` | `NUMERIC(38,18)` | Full precision |
| `bool` | `BOOLEAN` | Native boolean |
| `date` / `ISODate` | `TIMESTAMPTZ` | With timezone |
| `array` | `JSONB` | Queryable JSON |
| `object` (nested) | Flattened or `JSONB` | Sub-fields become columns |
| `binData` | `BYTEA` | Native binary |

### Nested Document Handling

Nested documents are flattened with dot notation converted to underscores:

```json
{"address": {"city": "NYC", "zip": "10001"}}
```
Becomes columns: `address_city` (TEXT), `address_zip` (TEXT)

---

## Output Files

```
migration_mongo_to_postgres_output/
├── audit.json              ← discovered schema + field types
├── migration_report.json   ← final row count validation
└── logs/
    └── migrate_mongo_to_postgres.log
```

---

## Verify the Migration

```sql
-- Connect
psql -h localhost -U postgres -d myapp_pg

-- List tables
\dt

-- Row counts
SELECT schemaname, relname, n_tup_ins
FROM pg_stat_user_tables ORDER BY relname;

-- Spot check
SELECT * FROM users LIMIT 5;

-- Query JSONB fields
SELECT tags->>'category' FROM products WHERE tags ? 'category';
```

---

## Known Limitations

| Feature | Status |
|---|---|
| Deeply nested arrays of objects | Stored as JSONB column |
| MongoDB `$ref` / DBRef | Stored as JSONB (no FK created) |
| Schema-less variation | Type widened to TEXT if mixed types detected |
| Large documents (>1GB) | May exceed PostgreSQL TOAST limit |
| GridFS files | Not migrated — use separate tooling |
| Capped collections | Migrated as regular tables |

---

## Troubleshooting

| Error | Solution |
|---|---|
| `ServerSelectionTimeoutError` | Check MongoDB is running |
| `Authentication failed` (MongoDB) | Verify `--src-user`, `--src-pass`, `--src-auth-db` |
| `duplicate key value violates unique constraint` | Use `--drop-target` |
| `could not connect to server` (PostgreSQL) | Check PG is running, `pg_hba.conf` allows connection |
| `psycopg2 not installed` | Run `pip install psycopg2-binary` |
| `database does not exist` | Use `--drop-target` to auto-create |
