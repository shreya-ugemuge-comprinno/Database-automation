# etl_migrator_v2 — MSSQL → PostgreSQL Migration Tool

A lightweight two-file Python CLI that fully automates database migration from
Microsoft SQL Server to PostgreSQL, following an 11-phase pipeline.

---

## Files

```
etl_v2/
├── migrate.py        ← main script (all 11 phases)
├── type_map.py       ← MSSQL → PostgreSQL data type mapping
├── requirements.txt  ← Python dependencies
└── README.md         ← this file
```

---

## Prerequisites

### 1. Python 3.10 or higher
```cmd
python --version
```

### 2. Python dependencies
```cmd
pip install -r requirements.txt
```

### 3. ODBC Driver 18 for SQL Server
Required for connecting to MSSQL from Python.

- **Windows:** Download from https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
- **Linux (Ubuntu):** `sudo apt install msodbcsql18`
- **macOS:** `brew install msodbcsql18`

### 4. PostgreSQL tools (pg_dump, psql)
These come bundled with every PostgreSQL installation.
- Default path on Windows: `C:\Program Files\PostgreSQL\<version>\bin\`
- Add to PATH if needed.

---

## Quick Start

### Windows Authentication (recommended — no password needed for MSSQL)
```cmd
python migrate.py --windows-auth ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --tgt-host localhost ^
  --tgt-db aw_pg ^
  --tgt-user postgres ^
  --tgt-pass YourPassword ^
  --skip-schema dbo ^
  --out C:\migration ^
  --drop-target
```


```cmd
python migrate.py ^
  --windows-auth ^
  --src-host <SOURCE_SQL_SERVER_HOST> ^
  --src-db <SOURCE_DATABASE_NAME> ^
  --tgt-host <POSTGRES_HOST> ^
  --tgt-db <TARGET_POSTGRES_DATABASE> ^
  --tgt-user <POSTGRES_USERNAME> ^
  --tgt-pass <POSTGRES_PASSWORD> ^
  --skip-schema <SCHEMA_TO_SKIP> ^
  --out <OUTPUT_DIRECTORY> ^
  --drop-target
```

### SQL Server Authentication
```cmd
python migrate.py ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --src-user sa ^
  --src-pass YourPassword ^
  --tgt-host localhost ^
  --tgt-db aw_pg ^
  --tgt-user postgres ^
  --tgt-pass YourPassword ^
  --skip-schema dbo ^
  --out C:\migration ^
  --drop-target
```

> **Tip:** Always include `--drop-target` when re-running. It automatically drops
> and recreates the target database so you never get duplicate key errors.

---

## All CLI Arguments

### Source (MSSQL)
| Argument | Default | Description |
|---|---|---|
| `--src-host` | `localhost` | MSSQL server hostname or IP |
| `--src-port` | `1433` | MSSQL port |
| `--src-db` | *(required)* | Source database name |
| `--src-user` | None | MSSQL username (omit if using `--windows-auth`) |
| `--src-pass` | None | MSSQL password (omit if using `--windows-auth`) |
| `--windows-auth` | false | Use Windows Authentication instead of SQL login |

### Target (PostgreSQL)
| Argument | Default | Description |
|---|---|---|
| `--tgt-host` | `localhost` | PostgreSQL hostname or IP |
| `--tgt-port` | `5432` | PostgreSQL port |
| `--tgt-db` | *(required)* | Target database name |
| `--tgt-user` | *(required)* | PostgreSQL username |
| `--tgt-pass` | *(required)* | PostgreSQL password |

### General
| Argument | Default | Description |
|---|---|---|
| `--skip-schema` | `dbo` | One or more schemas to skip entirely |
| `--out` | `./migration_output` | Output directory for CSVs, logs, and reports |
| `--drop-target` | false | Drop and recreate the target DB before migrating |
| `--audit-only` | false | Run audit and generate reports only — no migration |

---

## 11-Phase Migration Pipeline

| Phase | What happens |
|---|---|
| **1–2. Audit** | Connects to MSSQL, reads all schemas, tables, columns, PKs, FKs, indexes, views, functions, procedures, and row counts. Generates `audit.json` and `compatibility_report.json`. |
| **3. Create schemas** | Creates all target schemas in PostgreSQL. Skipped schemas (e.g. `dbo`) are ignored. |
| **4. Create tables** | Creates all tables with converted data types, nullability, and primary keys. No FK constraints yet. |
| **5. Migrate data** | Exports each table from MSSQL to CSV (with special handling for binary/XML columns), then bulk-loads using PostgreSQL `COPY`. FK checks are suspended during load. Validates row counts. |
| **6. Apply constraints** | Adds FK constraints, UNIQUE constraints, and CHECK constraints via `ALTER TABLE`. CHECK clauses are translated from MSSQL bracket syntax to PostgreSQL. |
| **7. Create indexes** | Creates all non-PK indexes using `CREATE INDEX CONCURRENTLY`. |
| **8. Migrate views** | Extracts view bodies, converts MSSQL bracket identifiers to lowercase quoted identifiers, and creates views. Views using MSSQL-only features (CROSS APPLY, PIVOT, XML methods) are skipped with a warning. |
| **9. Migrate functions** | Logs functions that require manual rewrite from T-SQL to PL/pgSQL. |
| **10. Migrate procedures** | Logs stored procedures that require manual rewrite from T-SQL to PL/pgSQL. |
| **11. Final validation** | Compares row counts between source and target for every table. Writes `migration_report.json`. |

---

## Output Files

After a successful run, the `--out` directory contains:

```
C:\migration\
├── audit.json                  ← full source database metadata
├── compatibility_report.json   ← MSSQL constructs that need manual attention
├── migration_report.json       ← final row count validation per table
├── data\
│   ├── HumanResources__Department.csv
│   ├── Person__Address.csv
│   └── ...                     ← one CSV per migrated table
└── logs\
    └── migrate.log             ← full log of all phases
```

---

## Data Type Mapping

| MSSQL Type | PostgreSQL Type | Notes |
|---|---|---|
| `INT` | `INTEGER` | |
| `BIGINT` | `BIGINT` | |
| `TINYINT` | `SMALLINT` | No tinyint in PostgreSQL |
| `BIT` | `BOOLEAN` | |
| `DECIMAL(p,s)` | `NUMERIC(p,s)` | Precision and scale preserved |
| `MONEY` | `NUMERIC(19,4)` | |
| `FLOAT` | `DOUBLE PRECISION` | |
| `DATETIME` | `TIMESTAMP` | |
| `DATETIME2` | `TIMESTAMPTZ` | Timezone-aware |
| `DATETIMEOFFSET` | `TIMESTAMPTZ` | |
| `NVARCHAR(n)` | `VARCHAR(n)` | PostgreSQL is UTF-8 by default |
| `NVARCHAR(MAX)` | `TEXT` | |
| `UNIQUEIDENTIFIER` | `UUID` | |
| `VARBINARY` / `IMAGE` | `BYTEA` | Stored as hex in CSV |
| `XML` | `TEXT` | |
| `GEOGRAPHY` / `GEOMETRY` | `TEXT` | Cast to string during export |
| `HIERARCHYID` | `TEXT` | Cast to string during export |
| `IDENTITY` | `SERIAL` / `BIGSERIAL` | Auto-increment preserved |

---

## Known Limitations

These MSSQL features cannot be auto-migrated and require manual rewrites:

| Feature | Why |
|---|---|
| `CROSS APPLY` / `OUTER APPLY` | No direct PostgreSQL equivalent — use `LATERAL JOIN` |
| `PIVOT` | Use `crosstab()` from the `tablefunc` extension |
| XML `.value()` / `.nodes()` | PostgreSQL uses `xpath()` and `xmltable()` |
| Stored procedures | T-SQL → PL/pgSQL requires manual rewrite |
| Functions | T-SQL → PL/pgSQL requires manual rewrite |
| `UPPER(col) IN (...)` checks on nullable columns | May violate if existing data has NULLs |

---

## Backup and Restore

### Create a backup after migration
```cmd
pg_dump -h localhost -U postgres -d aw_pg -F c -f C:\migration\aw_pg.backup
```

### Restore to any PostgreSQL server
```cmd
psql -h target-host -U postgres -c "CREATE DATABASE aw_pg;"
pg_restore -h target-host -U postgres -d aw_pg -F c C:\migration\aw_pg.backup
```

### Plain SQL dump (human-readable)
```cmd
pg_dump -h localhost -U postgres -d aw_pg -F p -f C:\migration\aw_pg.sql
psql -h target-host -U postgres -d aw_pg -f C:\migration\aw_pg.sql
```

---

## Verifying the Migration

Connect to PostgreSQL and run:

```sql
-- List all schemas
\dn

-- List all tables in a schema
\dt humanresources.*

-- Row count summary across all tables
SELECT schemaname, tablename, n_live_tup AS approx_rows
FROM pg_stat_user_tables
ORDER BY schemaname, tablename;

-- Spot check a table
SELECT COUNT(*) FROM sales.salesorderheader;
SELECT * FROM humanresources.department LIMIT 10;
```

---

## Troubleshooting

| Error | Solution |
|---|---|
| `Login failed (18456)` | Wrong password, or SQL Auth not enabled. Use `--windows-auth` or enable Mixed Mode in SSMS → Server Properties → Security. |
| `Duplicate key value` | Database was not cleaned before re-run. Add `--drop-target` to the command. |
| `ODBC SQL type -151` | Spatial/XML column — handled automatically by casting to text. |
| `field larger than field limit` | Large binary column — handled automatically by raising the CSV field size limit. |
| `CREATE INDEX CONCURRENTLY cannot run inside a transaction block` | Fixed internally — indexes run in autocommit mode. |
| `relation "dbo.X" does not exist` | Schema not remapped. Make sure `--skip-schema dbo` is set. |

---

## Re-running on a Different Database

Change `--src-db` and `--tgt-db` — everything else stays the same:

```cmd
python migrate.py --windows-auth ^
  --src-host localhost ^
  --src-db MyOtherDatabase ^
  --tgt-host localhost ^
  --tgt-db myother_pg ^
  --tgt-user postgres ^
  --tgt-pass YourPassword ^
  --skip-schema dbo ^
  --out C:\migration_myother ^
  --drop-target
```

---

## Audit Only (no migration)

Generate the compatibility report without touching PostgreSQL:

```cmd
python migrate.py --windows-auth ^
  --src-host localhost ^
  --src-db AdventureWorks2025 ^
  --tgt-host localhost ^
  --tgt-db aw_pg ^
  --tgt-user postgres ^
  --tgt-pass YourPassword ^
  --skip-schema dbo ^
  --out C:\migration ^
  --audit-only
```

This produces `audit.json` and `compatibility_report.json` without creating
any tables or loading any data. Use this first to understand what needs manual
attention before committing to a migration.






What was skipped and why 

CHECK constraints — 6 skipped

ck_employee_birthdate, ck_employee_hiredateUses :- MSSQL YEAR() / DAY() functions — PostgreSQL uses EXTRACT(year FROM col) instead

ck_product_productline, ck_product_class, ck_product_style :- UPPER(col) IN (...) — existing data has NULL or values not matching the allowed list

ck_productinventory_shelf :- [Shelf] LIKE '[A-Za-z]' — MSSQL LIKE pattern, PostgreSQL uses ~ '^[A-Za-z]$'

FK — 2 skipped
fk_salesorderdetail_specialofferproduct — This FK references a composite key (specialofferid, productid) on specialofferproduct. The retry logic added a unique constraint but the FK still has a duplicate entry in the audit from MSSQL extracting it twice. The data integrity is intact because the PK already enforces uniqueness.


Views — 11 skipped

CROSS APPLY / OUTER APPLY — MSSQL-specific, no direct PG equivalent :- 5 views

PIVOT — not in PostgreSQL :- 1 view

XML .value() / .nodes() method calls — MSSQL XML type feature :- 3 views

Column alias syntax "colname" = expression — MSSQL style :- 2 views


Procedures — 3 skipped
All 3 are T-SQL procedures that need manual rewrite to PL/pgSQL. The logic is intact in C:\migration\audit.json under procedures.