# etl_migrator — MSSQL → PostgreSQL Migration Tool

A production-grade CLI pipeline: **Extract → Transform → Audit → Load**

---

## Project structure

```
etl_migrator/
├── main.py                              ← CLI entry point (all 4 phases)
├── requirements.txt
├── .github/workflows/migration.yml      ← CI/CD pipeline (GitHub Actions)
│
├── extractor/
│   └── mssql_extractor.py              ← Phase 1: DDL metadata + CSV export
│
├── transformer/
│   └── ddl_transformer.py              ← Phase 2: Type mapping, DDL rewrite
│
├── auditor/
│   └── schema_auditor.py               ← Phase 3: Schema remap + compatibility scan
│
├── loader/
│   └── postgres_loader.py              ← Phase 4: COPY bulk-load + validation
│
└── utils/
    ├── logger.py                        ← Console + rotating file logging
    ├── type_mapping.py                  ← MSSQL → PostgreSQL type map (30+ types)
    └── flyway_versioner.py             ← Wrap schema.sql into Flyway V__.sql file
```

---

## Prerequisites

```cmd
pip install -r requirements.txt
```

Install **ODBC Driver 18 for SQL Server**:
- Windows: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
- Linux: `sudo apt install msodbcsql18`
- macOS: `brew install msodbcsql18`

---

## Usage

### Run all 4 phases in one command

**Windows CMD:**
```cmd
python main.py run-all ^
  --src-host localhost --src-db AdventureWorks --src-user sa --src-pass secret ^
  --tgt-host localhost --tgt-db aw_pg --tgt-user postgres --tgt-pass secret ^
  --schema dbo --remap-schema dbo=public --out C:\migration
```

**Linux / macOS:**
```bash
python main.py run-all \
  --src-host localhost --src-db AdventureWorks --src-user sa --src-pass secret \
  --tgt-host localhost --tgt-db aw_pg --tgt-user postgres --tgt-pass secret \
  --schema dbo --remap-schema dbo=public --out ./migration
```

---

### Run each phase separately

**1. Extract** — pull DDL + CSV data from MSSQL
```cmd
python main.py extract ^
  --src-host localhost --src-db MyDB --src-user sa --src-pass secret ^
  --schema dbo --out ./output/raw
```

**2. Transform** — rewrite DDL to PostgreSQL syntax
```cmd
python main.py transform ^
  --input ./output/raw --out ./output/transformed --target postgres
```

**3. Audit** — remap schema names + scan for incompatibilities
```cmd
python main.py audit ^
  --input ./output/transformed ^
  --out   ./output/audited ^
  --remap-schema dbo=public
```
Pipeline halts here if BLOCKERs are found. Use `--ignore-blockers` to override.

**4. Load** — create schema + bulk-load + validate
```cmd
python main.py load ^
  --tgt-host localhost --tgt-db aw_pg --tgt-user postgres --tgt-pass secret ^
  --input ./output/audited
```

---

## Schema remapping (--remap-schema)

| Example | Effect |
|---|---|
| `dbo=public` | Replace all `dbo.` refs with `public.` (PostgreSQL default) |
| `dbo=myapp` | Use a custom schema name `myapp` |
| `sales=reporting` | Remap any source schema name |

The remapper handles all three MSSQL quoting styles:
- `[dbo].[TableName]` → `public.TableName`
- `"dbo"."TableName"` → `public.TableName`
- `dbo.TableName` → `public.TableName`

String literals are **not** remapped (to preserve data values).
Hardcoded schema refs inside dynamic SQL strings are flagged as **BLOCKER**.

---

## Audit findings

Every finding is classified as:

| Severity | Meaning | Pipeline behaviour |
|---|---|---|
| **BLOCKER** | Cannot migrate without manual fix | Pipeline halts |
| **WARNING** | Should be reviewed / auto-fixable | Logged, continues |
| **INFO** | Auto-handled by type mapper | Logged only |

### BLOCKER examples
- Dynamic SQL via `EXEC()` or `sp_executesql`
- Temp tables (`#tablename`)
- `OPENROWSET` / `OPENQUERY`
- `FOR XML`
- Hardcoded schema names inside string literals

### WARNING examples
- `TOP n` → needs `LIMIT n`
- `ISNULL()` → needs `COALESCE()`
- `WITH (NOLOCK)` → remove
- `@@ROWCOUNT`, `@@IDENTITY`
- `RAISERROR`, `DATEADD`, `DATEDIFF`, `CONVERT`

---

## Output files

After a full run:

```
migration/
├── raw/
│   ├── manifest.json           ← MSSQL table metadata
│   └── data/Orders.csv         ← raw exported data
│
├── transformed/
│   ├── schema.sql              ← PostgreSQL DDL (before audit)
│   ├── manifest.json
│   └── data/Orders.csv         ← cleaned CSVs
│
└── audited/
    ├── schema.sql              ← Final DDL with schema remapping applied
    ├── schema.diff             ← Unified diff of what changed
    ├── compatibility_report.json ← All findings (blockers/warnings/info)
    ├── manifest.json           ← Updated with remapped schema names
    └── validation_report.json  ← Post-load row counts + NULL checks
```

---

## Flyway versioning

Wrap the audited schema into a versioned Flyway migration file:

```cmd
python -m utils.flyway_versioner ^
  --input ./migration/audited ^
  --out   ./flyway/migrations ^
  --description migrate_adventureworks
```

Produces: `flyway/migrations/V20240527_143022__migrate_adventureworks.sql`

Place this file in your Flyway `locations` directory. Flyway tracks which
versions have been applied via `flyway_schema_history` and will not re-run
the same migration twice.

---

## CI/CD (GitHub Actions)

The included `.github/workflows/migration.yml` runs on every PR:

1. **Lint** — syntax-check all Python files
2. **Audit pipeline** — extract from staging MSSQL → transform → audit
3. **Gate check** — fails the PR if any BLOCKERs are found
4. **PR comment** — posts the audit summary table as a GitHub PR comment
5. **Artifacts** — uploads `compatibility_report.json`, `schema.diff`, Flyway file

Required GitHub secrets:
```
MSSQL_STAGING_HOST
MSSQL_STAGING_DB
MSSQL_STAGING_USER
MSSQL_STAGING_PASS
```

---

## Data type mapping

| MSSQL | PostgreSQL | Notes |
|---|---|---|
| `tinyint` | `smallint` | No tinyint in PG |
| `int` | `integer` | |
| `bigint` | `bigint` | |
| `bit` | `boolean` | |
| `decimal(p,s)` | `numeric(p,s)` | Precision preserved |
| `money` | `numeric(19,4)` | |
| `datetime2` | `timestamptz` | Timezone-aware |
| `datetime` | `timestamp` | |
| `nvarchar(n)` | `varchar(n)` | PG is UTF-8 by default |
| `nvarchar(MAX)` | `text` | |
| `uniqueidentifier` | `uuid` | |
| `varbinary` | `bytea` | |
| `IDENTITY` | `serial`/`bigserial` | Auto-increment |
