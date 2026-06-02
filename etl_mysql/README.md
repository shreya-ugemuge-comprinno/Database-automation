# etl_migrator_mysql — MSSQL → MySQL Migration Tool

A lightweight two-file Python CLI that automates database migration from
Microsoft SQL Server to MySQL, following an 11-phase pipeline.

---

## Files

```
etl_mysql/
├── migrate_mysql.py  ← main script (all 11 phases)
├── type_map.py       ← MSSQL → MySQL data type mapping
├── requirements.txt  ← Python dependencies
└── README.md         ← this file
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

### 4. MySQL Server 8.0+ (target)
MySQL 8.0.16+ is required for CHECK constraint support.

---

## Quick Start

### Windows Authentication (recommended)
```cmd
python migrate_mysql.py --windows-auth ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --tgt-host localhost ^
  --tgt-db aw_mysql ^
  --tgt-user root ^
  --tgt-pass YourPassword ^
  --skip-schema dbo ^
  --out C:\migration_mysql ^
  --drop-target
```
```
python migrate_mysql.py ^
  --windows-auth ^
  --src-host <SOURCE_SQL_SERVER_HOST> ^
  --src-db <SOURCE_DATABASE_NAME> ^
  --tgt-host <MYSQL_HOST> ^
  --tgt-db <TARGET_DATABASE_NAME> ^
  --tgt-user <MYSQL_USERNAME> ^
  --tgt-pass <MYSQL_PASSWORD> ^
  --skip-schema <SCHEMA_TO_SKIP> ^
  --out <OUTPUT_DIRECTORY> ^
  --drop-target
  
```


### SQL Server Authentication
```cmd
python migrate_mysql.py ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --src-user sa ^
  --src-pass YourPassword ^
  --tgt-host localhost ^
  --tgt-db aw_mysql ^
  --tgt-user root ^
  --tgt-pass YourPassword ^
  --skip-schema dbo ^
  --out C:\migration_mysql ^
  --drop-target
```

> **Always use `--drop-target`** when re-running to avoid duplicate key errors.

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

### Target (MySQL)
| Argument | Default | Description |
|---|---|---|
| `--tgt-host` | `localhost` | MySQL hostname or IP |
| `--tgt-port` | `3306` | MySQL port |
| `--tgt-db` | *(required)* | Target database name |
| `--tgt-user` | *(required)* | MySQL username |
| `--tgt-pass` | *(required)* | MySQL password |

### General
| Argument | Default | Description |
|---|---|---|
| `--skip-schema` | `dbo` | Schemas to skip (e.g. dbo) |
| `--out` | `./migration_mysql_output` | Output directory |
| `--drop-target` | false | Drop and recreate target DB before migrating |
| `--audit-only` | false | Generate reports only, no migration |

---

## 11-Phase Pipeline

| Phase | What happens |
|---|---|
| **1–2. Audit** | Reads all tables, columns, PKs, FKs, indexes, views, procedures, and row counts from MSSQL. Generates `audit.json` and `compatibility_report.json`. |
| **3. Create database** | Creates the MySQL database with `utf8mb4` character set. |
| **4. Create tables** | Creates tables with converted data types, nullability, PKs, and `AUTO_INCREMENT`. No FKs yet. |
| **5. Migrate data** | Exports each table from MSSQL to CSV, then inserts using parameterised batch `INSERT`. FK checks suspended during load. Validates row counts. |
| **6. Apply constraints** | Adds FK, UNIQUE, and CHECK constraints via `ALTER TABLE`. |
| **7. Create indexes** | Creates non-PK indexes with prefix lengths for TEXT/BLOB columns. |
| **8. Migrate views** | Converts MSSQL bracket identifiers to MySQL backtick identifiers and creates views. |
| **9. Migrate functions** | Logs functions requiring manual T-SQL → MySQL rewrite. |
| **10. Migrate procedures** | Logs procedures requiring manual T-SQL → MySQL rewrite. |
| **11. Final validation** | Row count comparison per table. Writes `migration_report.json`. |

---

## Data Type Mapping

| MSSQL Type | MySQL Type | Notes |
|---|---|---|
| `INT` | `INT` | |
| `BIGINT` | `BIGINT` | |
| `TINYINT` | `TINYINT UNSIGNED` | MSSQL tinyint = 0-255 |
| `SMALLINT` | `SMALLINT` | |
| `BIT` | `TINYINT(1)` | MySQL has no BOOLEAN |
| `DECIMAL(p,s)` | `DECIMAL(p,s)` | Precision preserved |
| `MONEY` | `DECIMAL(19,4)` | |
| `FLOAT` | `DOUBLE` | |
| `DATETIME` | `DATETIME` | |
| `DATETIME2` | `DATETIME(6)` | Microsecond precision |
| `DATETIMEOFFSET` | `DATETIME(6)` | No timezone in MySQL |
| `NVARCHAR(n)` | `VARCHAR(n)` | utf8mb4 charset |
| `NVARCHAR(MAX)` | `LONGTEXT` | |
| `UNIQUEIDENTIFIER` | `CHAR(36)` | UUID stored as string |
| `VARBINARY` / `IMAGE` | `LONGBLOB` | |
| `XML` | `LONGTEXT` | |
| `GEOGRAPHY` / `GEOMETRY` | `LONGTEXT` | Cast to text |
| `IDENTITY` | `BIGINT AUTO_INCREMENT` | |

---

## Key Differences vs PostgreSQL Migration

| Feature | PostgreSQL | MySQL |
|---|---|---|
| Schema concept | `schema.table` | Database = schema; no sub-schemas |
| `dbo` remapping | Maps to `public` schema | Dropped entirely (no schemas in MySQL) |
| Boolean | `BOOLEAN` | `TINYINT(1)` |
| UUID | `UUID` native type | `CHAR(36)` string |
| Data loading | `COPY FROM STDIN` | Batch `INSERT` (500 rows/batch) |
| Index on TEXT | No prefix needed | Requires prefix length (191 chars) |
| Timezone | `TIMESTAMPTZ` | `DATETIME(6)` — no tz stored |

---

## Output Files

```
C:\migration_mysql\
├── audit.json                  ← full source metadata
├── compatibility_report.json   ← MSSQL constructs needing manual work
├── migration_report.json       ← final row count validation
├── data\
│   ├── HumanResources__Department.csv
│   └── ...
└── logs\
    └── migrate_mysql.log
```

---

## Backup and Restore

### Backup the migrated database
```cmd
mysqldump -h localhost -u root -p aw_mysql > C:\migration_mysql\aw_mysql.sql
```

### Restore on any MySQL server
```cmd
mysql -h target-host -u root -p -e "CREATE DATABASE aw_mysql CHARACTER SET utf8mb4;"
mysql -h target-host -u root -p aw_mysql < C:\migration_mysql\aw_mysql.sql
```

### Compressed backup
```cmd
mysqldump -h localhost -u root -p aw_mysql | gzip > aw_mysql.sql.gz
```

---

## Verify the Migration

```sql
-- Connect
mysql -h localhost -u root -p aw_mysql

-- List all tables
SHOW TABLES;

-- Row count summary
SELECT table_name, table_rows
FROM information_schema.tables
WHERE table_schema = 'aw_mysql'
ORDER BY table_name;

-- Spot check
SELECT COUNT(*) FROM humanresources_department;
SELECT * FROM humanresources_department LIMIT 5;
```

---

## Known Limitations

| Feature | Status |
|---|---|
| `CROSS APPLY` / `OUTER APPLY` in views | Skipped — rewrite as `LATERAL JOIN` |
| `PIVOT` | Skipped — rewrite as `CASE` expressions |
| XML `.value()` / `.nodes()` | Skipped — no equivalent |
| Stored procedures | Logged only — manual T-SQL → MySQL rewrite needed |
| Functions | Logged only — manual T-SQL → MySQL rewrite needed |
| Timezone-aware datetimes | Stored as `DATETIME(6)` — timezone info lost |
| `DATETIMEOFFSET` precision | Truncated to microseconds |

---

## Troubleshooting

| Error | Solution |
|---|---|
| `Login failed (18456)` | Use `--windows-auth` or enable Mixed Mode in SSMS |
| `Duplicate key value` | Add `--drop-target` to the command |
| `Access denied for user 'root'` | Check MySQL credentials; root may need `--tgt-host 127.0.0.1` instead of `localhost` |
| `mysql-connector-python not installed` | Run `pip install mysql-connector-python` |
| `ODBC SQL type -151` | Spatial column — auto-cast to text |
| Index on TEXT column fails | Auto-handled with 191-char prefix length |
| CHECK constraint skipped | MySQL 8.0.16+ required; older versions ignore CHECK |
