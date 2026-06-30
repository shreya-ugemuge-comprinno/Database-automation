# etl_migration_mongo_to_mysql — MongoDB → MySQL Migration Tool

A lightweight two-file Python CLI that automates database migration from
MongoDB to MySQL, following a 7-phase pipeline.

---

## Files

```
etl_migration_mongo_to_mysql/
├── migrate_mongo_to_mysql.py  ← main script (all 7 phases)
├── type_map.py                ← MongoDB BSON → MySQL type mapping
├── requirements.txt           ← Python dependencies
└── README.md                  ← this file
```

---

## Prerequisites

### 1. Python 3.10+
### 2. Python dependencies
```cmd
pip install -r requirements.txt
```
### 3. MongoDB Server (source)
### 4. MySQL Server 8.0+ (target)

---

## Quick Start

### Local MongoDB (no auth)
```cmd
python migrate_mongo_to_mysql.py ^
  --src-host localhost --src-db myapp ^
  --tgt-host localhost --tgt-db myapp_mysql ^
  --tgt-user root --tgt-pass YourPassword ^
  --out ./migration_output --drop-target
```

### With MongoDB Authentication
```cmd
python migrate_mongo_to_mysql.py ^
  --src-host localhost --src-db myapp ^
  --src-user mongoUser --src-pass mongoPass --src-auth-db admin ^
  --tgt-host localhost --tgt-db myapp_mysql ^
  --tgt-user root --tgt-pass YourPassword ^
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

### Target (MySQL)
| Argument | Default | Description |
|---|---|---|
| `--tgt-host` | `localhost` | MySQL hostname |
| `--tgt-port` | `3306` | MySQL port |
| `--tgt-db` | *(required)* | Target database name |
| `--tgt-user` | *(required)* | MySQL username |
| `--tgt-pass` | *(required)* | MySQL password |

### General
| Argument | Default | Description |
|---|---|---|
| `--skip-collections` | (none) | Collections to skip |
| `--sample-size` | `0` (full scan) | Docs to sample for schema inference |
| `--out` | `./migration_mongo_to_mysql_output` | Output directory |
| `--drop-target` | false | Drop target DB before migrating |
| `--audit-only` | false | Schema discovery only, no migration |

---

## 7-Phase Pipeline

| Phase | What happens |
|---|---|
| **1–2. Discovery** | Scans all collections, infers field types from all documents (or sample). Flattens nested sub-documents into dotted fields. Generates `audit.json`. |
| **3. Create DB + tables** | Creates MySQL database and one table per collection with inferred column types. `_id` field becomes the PRIMARY KEY. |
| **4. Migrate data** | Reads documents from MongoDB, converts BSON values to MySQL-compatible types, inserts in batches of 500. Validates row counts. |
| **5. Indexes** | Recreates MongoDB indexes as MySQL indexes (unique and non-unique). |
| **6–7. Validation** | Row count comparison per table. Writes `migration_report.json`. |

---

## Data Type Mapping

| MongoDB/BSON Type | MySQL Type | Notes |
|---|---|---|
| `ObjectId` | `CHAR(24)` | Hex string |
| `string` | `LONGTEXT` | |
| `int` (32-bit) | `INT` | |
| `long` (64-bit) | `BIGINT` | |
| `double` | `DOUBLE` | |
| `Decimal128` | `DECIMAL(38,18)` | Full precision |
| `bool` | `TINYINT(1)` | |
| `date` / `ISODate` | `DATETIME(6)` | |
| `array` | `JSON` | Stored as JSON |
| `object` (nested) | Flattened | Sub-fields become columns |
| `binData` | `LONGBLOB` | |
| `null` | `LONGTEXT` | Column allows NULL |

### Nested Document Handling

Nested documents are flattened with dot notation converted to underscores:

```json
{"address": {"city": "NYC", "zip": "10001"}}
```
Becomes columns: `address_city` (LONGTEXT), `address_zip` (LONGTEXT)

---

## Output Files

```
migration_mongo_to_mysql_output/
├── audit.json              ← discovered schema + field types
├── migration_report.json   ← final row count validation
└── logs/
    └── migrate_mongo_to_mysql.log
```

---

## Verify the Migration

```sql
mysql -h localhost -u root -p myapp_mysql

SHOW TABLES;

SELECT table_name, table_rows
FROM information_schema.tables
WHERE table_schema = 'myapp_mysql'
ORDER BY table_name;

SELECT * FROM users LIMIT 5;
```

---

## Known Limitations

| Feature | Status |
|---|---|
| Deeply nested arrays of objects | Stored as JSON column |
| MongoDB `$ref` / DBRef | Stored as JSON (no FK created) |
| Schema-less variation | Type widened to LONGTEXT if mixed types detected |
| Large documents (>16MB) | May exceed MySQL `max_allowed_packet` |
| GridFS files | Not migrated — use separate tooling |
| Capped collections | Migrated as regular tables |

---

## Troubleshooting

| Error | Solution |
|---|---|
| `ServerSelectionTimeoutError` | Check MongoDB is running |
| `Authentication failed` | Verify `--src-user`, `--src-pass`, `--src-auth-db` |
| `Duplicate entry for key PRIMARY` | Use `--drop-target` |
| `Data too long for column` | Schema inference may need `--sample-size 0` (full scan) |
| `max_allowed_packet exceeded` | Increase MySQL `max_allowed_packet` setting |
