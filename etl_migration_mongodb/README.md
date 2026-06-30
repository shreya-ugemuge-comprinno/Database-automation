# etl_migration_mongodb — MSSQL → MongoDB Migration Tool

A lightweight two-file Python CLI that automates database migration from
Microsoft SQL Server to MongoDB, following an 8-phase pipeline.

---

## Files

```
etl_migration_mongodb/
├── migrate_mongodb.py  ← main script (all 8 phases)
├── type_map.py         ← MSSQL → MongoDB/BSON type mapping
├── requirements.txt    ← Python dependencies
└── README.md           ← this file
```

---

## Prerequisites

### 1. Python 3.10+
```cmd
python --version
```

### 2. Python dependencies
```cmd
pip install -r requirements.txt
```

### 3. ODBC Driver 18 for SQL Server
- **Windows:** https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
- **Linux:** `sudo apt install msodbcsql18`
- **macOS:** `brew install msodbcsql18`

### 4. MongoDB Server 6.0+ (target)

---

## Quick Start

### Windows Authentication (recommended)
```cmd
python migrate_mongodb.py --windows-auth ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --tgt-host localhost ^
  --tgt-db aw_mongo ^
  --skip-schema dbo ^
  --out C:\migration_mongodb ^
  --drop-target
```

### SQL Server Authentication
```cmd
python migrate_mongodb.py ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --src-user sa ^
  --src-pass YourPassword ^
  --tgt-host localhost ^
  --tgt-db aw_mongo ^
  --skip-schema dbo ^
  --out C:\migration_mongodb ^
  --drop-target
```

### With MongoDB Authentication
```cmd
python migrate_mongodb.py --windows-auth ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --tgt-host localhost ^
  --tgt-db aw_mongo ^
  --tgt-user mongoUser ^
  --tgt-pass mongoPass ^
  --tgt-auth-db admin ^
  --skip-schema dbo ^
  --out C:\migration_mongodb ^
  --drop-target
```

> **Always use `--drop-target`** when re-running to avoid duplicate documents.

---

## All CLI Arguments

### Source (MSSQL)
| Argument | Default | Description |
|---|---|---|
| `--src-host` | `localhost` | MSSQL hostname or IP |
| `--src-port` | `1433` | MSSQL port |
| `--src-db` | *(required)* | Source database name |
| `--src-user` | None | MSSQL username (omit with `--windows-auth`) |
| `--src-pass` | None | MSSQL password (omit with `--windows-auth`) |
| `--windows-auth` | false | Use Windows Authentication |

### Target (MongoDB)
| Argument | Default | Description |
|---|---|---|
| `--tgt-host` | `localhost` | MongoDB hostname or IP |
| `--tgt-port` | `27017` | MongoDB port |
| `--tgt-db` | *(required)* | Target database name |
| `--tgt-user` | None | MongoDB username (omit for no auth) |
| `--tgt-pass` | None | MongoDB password (omit for no auth) |
| `--tgt-auth-db` | `admin` | MongoDB authentication database |

### General
| Argument | Default | Description |
|---|---|---|
| `--skip-schema` | `dbo` | MSSQL schemas to skip |
| `--out` | `./migration_mongodb_output` | Output directory |
| `--drop-target` | false | Drop target database before migrating |
| `--audit-only` | false | Generate reports only, no migration |

---

## 8-Phase Pipeline

| Phase | What happens |
|---|---|
| **1–2. Audit** | Reads all tables, columns, PKs, FKs, indexes, views, procedures, and row counts from MSSQL. Generates `audit.json` and `compatibility_report.json`. |
| **3. Create collections** | Creates MongoDB collections for each source table (named `schema_table`). |
| **4. Migrate data** | Reads rows from MSSQL, converts values to BSON-compatible types, inserts as documents in batches of 1000. Validates document counts. |
| **5. Create indexes** | Creates unique and non-unique indexes matching source constraints. |
| **6. Log views** | Stores view definitions in `_migration_views` collection for manual conversion to aggregation pipelines. |
| **7. Log functions/procedures** | Logs T-SQL objects requiring manual rewrite to application logic. |
| **8. Final validation** | Document count comparison per collection. Writes `migration_report.json`. |

---

## Data Type Mapping

| MSSQL Type | MongoDB/BSON Type | Notes |
|---|---|---|
| `INT`, `SMALLINT`, `TINYINT` | `int` | Native 32-bit integer |
| `BIGINT` | `long` | 64-bit integer |
| `BIT` | `bool` | Native boolean |
| `DECIMAL`, `NUMERIC`, `MONEY` | `Decimal128` | Full precision preserved |
| `FLOAT`, `REAL` | `double` | Native double |
| `DATETIME`, `DATETIME2` | `date` | ISODate in MongoDB |
| `DATE` | `date` | ISODate (time = 00:00) |
| `TIME` | `string` | Stored as string |
| `DATETIMEOFFSET` | `date` | Timezone offset lost |
| `VARCHAR`, `NVARCHAR`, `TEXT` | `string` | Native string |
| `UNIQUEIDENTIFIER` | `string` | UUID as string |
| `BINARY`, `VARBINARY`, `IMAGE` | `binData` | Native binary |
| `XML`, `GEOGRAPHY`, `GEOMETRY` | `string` | Cast to text |
| `IDENTITY` columns | `int`/`long` | Stored as regular field |

---

## Document Structure

Each MSSQL row becomes a MongoDB document. Column names are lowercased to field names:

```json
// MSSQL: HumanResources.Department (DepartmentID=1, Name='Engineering', ...)
// MongoDB collection: humanresources_department
{
  "_id": ObjectId("..."),
  "departmentid": 1,
  "name": "Engineering",
  "groupname": "Research and Development",
  "modifieddate": ISODate("2008-04-30T00:00:00Z")
}
```

---

## Output Files

```
migration_mongodb_output/
├── audit.json                  ← full source metadata
├── compatibility_report.json   ← MSSQL constructs needing manual work
├── migration_report.json       ← final document count validation
└── logs/
    └── migrate_mongodb.log
```

---

## Verify the Migration

```javascript
// Connect with mongosh
mongosh "mongodb://localhost:27017/aw_mongo"

// List collections
show collections

// Document counts
db.getCollectionNames().forEach(c => print(c + ": " + db[c].countDocuments()))

// Spot check
db.humanresources_department.find().limit(5).pretty()
```

---

## Known Limitations

| Feature | Status |
|---|---|
| Foreign keys | Logged in audit; no enforcement in MongoDB (by design) |
| CHECK constraints | Not applicable to MongoDB's schema-free model |
| Views | Stored in `_migration_views`; rewrite as aggregation pipelines |
| Stored procedures | Logged only — rewrite as application logic |
| Functions | Logged only — rewrite as application logic or `$function` |
| Timezone-aware datetimes | Stored as ISODate — timezone offset lost |
| Schema validation | Not auto-generated; add manually via `db.createCollection()` with `$jsonSchema` |

---

## Troubleshooting

| Error | Solution |
|---|---|
| `Login failed (18456)` | Use `--windows-auth` or enable Mixed Mode in SSMS |
| `Duplicate documents` | Add `--drop-target` to the command |
| `pymongo.errors.ServerSelectionTimeoutError` | Check MongoDB is running and accessible |
| `pymongo not installed` | Run `pip install pymongo` |
| `ODBC SQL type -151` | Spatial column — auto-cast to text |
| `Authentication failed` | Check `--tgt-user`, `--tgt-pass`, and `--tgt-auth-db` |
