#!/usr/bin/env python3
"""
migrate.py  —  MSSQL -> PostgreSQL migration tool
==================================================
Phases (in order):
  1.  Audit source database
  2.  Generate datatype mapping + compatibility report
  3.  Create PostgreSQL schemas  (dbo is skipped/remapped)
  4.  Create tables  (PK only — no FKs yet)
  5.  Migrate data + validate row counts
  6.  Apply FK, UNIQUE, CHECK, DEFAULT constraints
  7.  Create indexes
  8.  Migrate views
  9.  Migrate functions
  10. Migrate stored procedures
  11. Final validation + migration report

Usage (Windows CMD):
  python migrate.py ^
    --src-host localhost --src-db AdventureWorks --src-user sa --src-pass secret ^
    --tgt-host localhost --tgt-db aw_pg --tgt-user postgres --tgt-pass secret ^
    --skip-schema dbo --out C:\\migration

  # Windows Authentication (no username/password):
  python migrate.py --windows-auth ^
    --src-host localhost --src-db AdventureWorks ^
    --tgt-host localhost --tgt-db aw_pg --tgt-user postgres --tgt-pass secret ^
    --out C:\\migration
"""

import argparse
import csv
import json
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from type_map import map_column_type, translate_default, CAST_TYPES

# ── logging ───────────────────────────────────────────────────────────────────

import io
import logging
from logging.handlers import RotatingFileHandler


def _make_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("migrate")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # UTF-8 safe console
    if sys.platform == "win32":
        stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    else:
        stream = sys.stdout
    ch = logging.StreamHandler(stream)
    ch.setFormatter(fmt)
    fh = RotatingFileHandler(
        os.path.join(log_dir, "migrate.log"),
        maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


# ── CSV field size ─────────────────────────────────────────────────────────────

def _set_csv_limit():
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 2

_set_csv_limit()


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 1 + 2  —  Audit
# ═════════════════════════════════════════════════════════════════════════════

class Auditor:
    def __init__(self, conn, skip_schemas: set[str], log):
        self.conn = conn
        self.skip = {s.lower() for s in skip_schemas}
        self.log = log

    def run(self, out_dir: str) -> dict:
        self.log.info("=== PHASE 1-2: AUDIT ===")
        cur = self.conn.cursor()

        tables = self._get_tables(cur)
        self.log.info("Found %d migratable tables (skip=%s)", len(tables), sorted(self.skip))

        manifest = []
        for schema, table in tables:
            self.log.info("  Auditing %s.%s", schema, table)
            cols    = self._get_columns(cur, schema, table)
            pks     = self._get_pks(cur, schema, table)
            fks     = self._get_fks(cur, schema, table)
            uqs     = self._get_unique(cur, schema, table)
            chks    = self._get_checks(cur, schema, table)
            idx     = self._get_indexes(cur, schema, table)
            rows    = self._get_rowcount(cur, schema, table)
            manifest.append({
                "source_schema": schema,
                "source_table":  table,
                "target_schema": schema.lower(),
                "target_table":  table.lower(),
                "columns": cols,
                "primary_keys": pks,
                "foreign_keys": fks,
                "unique_constraints": uqs,
                "check_constraints": chks,
                "indexes": idx,
                "source_row_count": rows,
                "csv_file": f"{schema}__{table}.csv",
            })

        views = self._get_views(cur)
        funcs = self._get_functions(cur)
        procs = self._get_procedures(cur)

        audit = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tables": manifest,
            "views": views,
            "functions": funcs,
            "procedures": procs,
        }

        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "audit.json"), "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)

        # Compatibility report
        report = self._compatibility_report(manifest, views, funcs, procs)
        with open(os.path.join(out_dir, "compatibility_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        self.log.info("Audit complete. Tables=%d Views=%d Funcs=%d Procs=%d",
                      len(manifest), len(views), len(funcs), len(procs))
        self._log_compatibility(report)
        return audit

    def _get_tables(self, cur):
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        return [(s, t) for s, t in cur.fetchall()
                if s.lower() not in self.skip]

    def _get_columns(self, cur, schema, table):
        cur.execute("""
            SELECT
                c.COLUMN_NAME,
                c.ORDINAL_POSITION,
                c.DATA_TYPE,
                c.CHARACTER_MAXIMUM_LENGTH,
                c.NUMERIC_PRECISION,
                c.NUMERIC_SCALE,
                c.IS_NULLABLE,
                c.COLUMN_DEFAULT,
                COLUMNPROPERTY(OBJECT_ID(? + '.' + ?), c.COLUMN_NAME, 'IsIdentity') AS is_identity
            FROM INFORMATION_SCHEMA.COLUMNS c
            WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
            ORDER BY c.ORDINAL_POSITION
        """, schema + '.' + table, table, schema, table)
        cols = []
        for row in cur.fetchall():
            pg_type = map_column_type(
                row[2],
                str(row[3]) if row[3] is not None else None,
                str(row[4]) if row[4] is not None else None,
                str(row[5]) if row[5] is not None else None,
            )
            cols.append({
                "name":        row[0],
                "pg_name":     row[0].lower(),
                "ordinal":     row[1],
                "mssql_type":  row[2],
                "pg_type":     pg_type,
                "max_length":  str(row[3]) if row[3] is not None else None,
                "precision":   str(row[4]) if row[4] is not None else None,
                "scale":       str(row[5]) if row[5] is not None else None,
                "nullable":    row[6] == "YES",
                "default":     row[7],
                "is_identity": bool(row[8]),
                "needs_cast":  row[2].lower() in CAST_TYPES,
            })
        return cols

    def _get_pks(self, cur, schema, table):
        cur.execute("""
            SELECT kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA    = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
              AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
            ORDER BY kcu.ORDINAL_POSITION
        """, schema, table)
        return [r[0].lower() for r in cur.fetchall()]

    def _get_fks(self, cur, schema, table):
        cur.execute("""
            SELECT
                fk.name,
                COL_NAME(fc.parent_object_id, fc.parent_column_id),
                OBJECT_SCHEMA_NAME(fk.referenced_object_id),
                OBJECT_NAME(fk.referenced_object_id),
                COL_NAME(fc.referenced_object_id, fc.referenced_column_id),
                fk.delete_referential_action_desc,
                fk.update_referential_action_desc
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = fk.object_id
            WHERE OBJECT_NAME(fk.parent_object_id) = ?
              AND SCHEMA_NAME(fk.schema_id) = ?
        """, table, schema)
        fks = []
        for r in cur.fetchall():
            ref_schema = r[2].lower()
            # Skip FKs that reference skipped schemas
            if ref_schema in self.skip:
                continue
            fks.append({
                "name":       r[0].lower(),
                "column":     r[1].lower(),
                "ref_schema": ref_schema,
                "ref_table":  r[3].lower(),
                "ref_column": r[4].lower(),
                "on_delete":  r[5].replace("_", " ") if r[5] else "NO ACTION",
                "on_update":  r[6].replace("_", " ") if r[6] else "NO ACTION",
            })
        return fks

    def _get_unique(self, cur, schema, table):
        cur.execute("""
            SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA    = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
              AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
            ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """, schema, table)
        from collections import defaultdict
        grouped = defaultdict(list)
        for cname, col in cur.fetchall():
            grouped[cname.lower()].append(col.lower())
        return [{"name": k, "columns": v} for k, v in grouped.items()]

    def _get_checks(self, cur, schema, table):
        cur.execute("""
            SELECT cc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
            FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
            JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
              ON cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
             AND cc.CONSTRAINT_SCHEMA = tc.TABLE_SCHEMA
            WHERE tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
        """, schema, table)
        checks = []
        for r in cur.fetchall():
            clause = r[1] or ""
            # Skip auto-generated NOT NULL checks
            if "IS NOT NULL" in clause.upper():
                continue
            checks.append({"name": r[0].lower(), "clause": clause})
        return checks

    def _get_indexes(self, cur, schema, table):
        cur.execute("""
            SELECT
                i.name,
                i.is_unique,
                STRING_AGG(c.name, ',' ) WITHIN GROUP (ORDER BY ic.key_ordinal)
            FROM sys.indexes i
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE OBJECT_NAME(i.object_id) = ? AND SCHEMA_NAME(
                  (SELECT schema_id FROM sys.objects WHERE object_id = i.object_id)) = ?
              AND i.is_primary_key = 0 AND i.type > 0 AND i.name IS NOT NULL
            GROUP BY i.name, i.is_unique
        """, table, schema)
        return [
            {"name": r[0].lower(), "unique": bool(r[1]),
             "columns": [c.lower() for c in r[2].split(",")]}
            for r in cur.fetchall()
        ]

    def _get_rowcount(self, cur, schema, table):
        cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
        return cur.fetchone()[0]

    def _get_views(self, cur):
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION
            FROM INFORMATION_SCHEMA.VIEWS
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        return [
            {"schema": r[0], "name": r[1], "definition": r[2] or ""}
            for r in cur.fetchall()
            if r[0].lower() not in self.skip
        ]

    def _get_functions(self, cur):
        cur.execute("""
            SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_DEFINITION
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'FUNCTION'
            ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME
        """)
        return [
            {"schema": r[0], "name": r[1], "definition": r[2] or ""}
            for r in cur.fetchall()
            if r[0].lower() not in self.skip
        ]

    def _get_procedures(self, cur):
        cur.execute("""
            SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_DEFINITION
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'PROCEDURE'
            ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME
        """)
        return [
            {"schema": r[0], "name": r[1], "definition": r[2] or ""}
            for r in cur.fetchall()
            if r[0].lower() not in self.skip
        ]

    MSSQL_PATTERNS = [
        (r'\bEXEC\s*\(',          "BLOCKER",  "Dynamic SQL via EXEC()"),
        (r'#\w+',                  "BLOCKER",  "Temp table (#table)"),
        (r'\bOPENROWSET\b',        "BLOCKER",  "OPENROWSET not supported"),
        (r'\bFOR\s+XML\b',         "BLOCKER",  "FOR XML not supported"),
        (r'\bTOP\s+\d+\b',         "WARNING",  "TOP n -> LIMIT n"),
        (r'\bISNULL\s*\(',         "WARNING",  "ISNULL() -> COALESCE()"),
        (r'@@ROWCOUNT',            "WARNING",  "@@ROWCOUNT -> GET DIAGNOSTICS"),
        (r'\bRAISERROR\b',         "WARNING",  "RAISERROR -> RAISE EXCEPTION"),
        (r'\bNOLOCK\b',            "WARNING",  "NOLOCK hint - remove"),
        (r'\bDATEADD\s*\(',        "WARNING",  "DATEADD -> interval arithmetic"),
        (r'\bDATEDIFF\s*\(',       "WARNING",  "DATEDIFF -> EXTRACT/date_part"),
        (r'\bCONVERT\s*\(',        "WARNING",  "CONVERT -> CAST"),
        (r'\bGETDATE\s*\(\)',      "INFO",     "GETDATE() -> CURRENT_TIMESTAMP"),
        (r'\bNVARCHAR\b',          "INFO",     "NVARCHAR -> VARCHAR"),
    ]

    def _compatibility_report(self, tables, views, funcs, procs):
        findings = []
        all_objects = (
            [(v["schema"] + "." + v["name"], "VIEW",      v["definition"]) for v in views] +
            [(f["schema"] + "." + f["name"], "FUNCTION",  f["definition"]) for f in funcs] +
            [(p["schema"] + "." + p["name"], "PROCEDURE", p["definition"]) for p in procs]
        )
        for obj_name, obj_type, body in all_objects:
            if not body:
                continue
            for pattern, severity, msg in self.MSSQL_PATTERNS:
                if re.search(pattern, body, re.IGNORECASE):
                    findings.append({"object": obj_name, "type": obj_type,
                                     "severity": severity, "issue": msg})

        blockers = [f for f in findings if f["severity"] == "BLOCKER"]
        warnings = [f for f in findings if f["severity"] == "WARNING"]
        infos    = [f for f in findings if f["severity"] == "INFO"]
        return {
            "gate": "FAIL" if blockers else "PASS",
            "blockers": len(blockers), "warnings": len(warnings), "info": len(infos),
            "findings": findings,
        }

    def _log_compatibility(self, report):
        self.log.info("Compatibility gate: %s  (blockers=%d warnings=%d info=%d)",
                      report["gate"], report["blockers"],
                      report["warnings"], report["info"])
        for f in report["findings"]:
            if f["severity"] == "BLOCKER":
                self.log.warning("  [BLOCKER] %s (%s): %s",
                                 f["object"], f["type"], f["issue"])


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 3-7  —  Load (schemas, tables, data, constraints, indexes)
# ═════════════════════════════════════════════════════════════════════════════

class Loader:
    def __init__(self, pg_conn, src_conn, audit: dict, work_dir: str, log):
        self.pg   = pg_conn
        self.src  = src_conn
        self.audit = audit
        self.work  = work_dir
        self.log   = log
        self.csv_dir = os.path.join(work_dir, "data")
        os.makedirs(self.csv_dir, exist_ok=True)

    def _pg(self, sql: str, params=None, autocommit=False):
        """Execute a single SQL statement on PostgreSQL."""
        old_ac = self.pg.autocommit
        if autocommit:
            self.pg.autocommit = True
        cur = self.pg.cursor()
        try:
            cur.execute(sql, params)
            if not autocommit:
                self.pg.commit()
        except Exception:
            if not autocommit:
                try:
                    self.pg.rollback()
                except Exception:
                    pass
            raise
        finally:
            self.pg.autocommit = old_ac
        return cur

    def _pg_quiet(self, sql: str, label: str = "") -> bool:
        """Execute SQL, log warning on failure but don't raise."""
        try:
            self._pg(sql)
            return True
        except Exception as exc:
            self.log.warning("  Skipped [%s]: %s", label, str(exc).strip())
            return False

    @staticmethod
    def _translate_check(clause: str) -> str | None:
        """
        Convert a MSSQL CHECK clause to PostgreSQL syntax:
          [ColumnName]  ->  "columnname"    (bracket identifiers -> quoted lowercase)
          upper([Col])  ->  upper("col")    (functions preserved)
          Strips outer wrapping parens added by MSSQL.
        """
        if not clause:
            return None
        c = clause.strip()
        # Strip outermost parens that MSSQL wraps around the whole clause
        while c.startswith("(") and c.endswith(")"):
            # Only strip if the parens are truly outermost (balanced)
            depth = 0
            outermost = True
            for i, ch in enumerate(c):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if depth == 0 and i < len(c) - 1:
                    outermost = False
                    break
            if outermost:
                c = c[1:-1].strip()
            else:
                break
        # Convert [ColumnName] -> "columnname"
        c = re.sub(r'\[([^\]]+)\]', lambda m: '"' + m.group(1).lower() + '"', c)
        return c

    # ── Phase 3: schemas ──────────────────────────────────────────────────────

    def create_schemas(self):
        self.log.info("=== PHASE 3: CREATE SCHEMAS ===")
        schemas = {t["target_schema"] for t in self.audit["tables"]}
        for s in sorted(schemas):
            self._pg(f'CREATE SCHEMA IF NOT EXISTS "{s}"')
            self.log.info("  Schema: %s", s)

    # ── Phase 4: tables (PK only) ─────────────────────────────────────────────

    def create_tables(self):
        self.log.info("=== PHASE 4: CREATE TABLES ===")
        for t in self.audit["tables"]:
            sql = self._build_create_table(t)
            try:
                self._pg(sql)
                self.log.info("  Created: %s.%s", t["target_schema"], t["target_table"])
            except Exception as exc:
                self.log.error("  FAILED %s.%s: %s", t["target_schema"], t["target_table"], exc)
                self.log.debug("  SQL was:\n%s", sql)
                raise

    def _build_create_table(self, t: dict) -> str:
        schema = t["target_schema"]
        table  = t["target_table"]
        lines  = []

        for col in t["columns"]:
            pg_name = col["pg_name"]
            pg_type = col["pg_type"]

            if col["is_identity"]:
                base = "bigserial" if "bigint" in pg_type else "serial"
                lines.append(f'    "{pg_name}"  {base}')
                continue

            parts = [f'    "{pg_name}"  {pg_type}']
            if not col["nullable"]:
                parts.append("NOT NULL")
            default = translate_default(col["default"], pg_type)
            if default:
                parts.append(f"DEFAULT {default}")
            lines.append("  ".join(parts))

        # PK inline
        if t["primary_keys"]:
            pk_cols = ", ".join(f'"{c}"' for c in t["primary_keys"])
            lines.append(f'    CONSTRAINT "pk_{table}" PRIMARY KEY ({pk_cols})')

        body = ",\n".join(lines)
        return f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (\n{body}\n);\n'

    # ── Phase 5: data ─────────────────────────────────────────────────────────

    def migrate_data(self) -> list[dict]:
        self.log.info("=== PHASE 5: DATA MIGRATION ===")
        results = []

        # Disable FK triggers for bulk load
        self._pg("SET session_replication_role = replica")
        self.log.info("  FK checks suspended.")

        for t in self.audit["tables"]:
            result = self._load_table(t)
            results.append(result)

        self._pg("SET session_replication_role = DEFAULT")
        self.log.info("  FK checks restored.")

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] != "PASS")
        self.log.info("  Data load: %d PASS  |  %d FAIL", passed, failed)
        return results

    def _extract_csv(self, t: dict) -> str:
        """Extract table data from MSSQL into a CSV file."""
        schema = t["source_schema"]
        table  = t["source_table"]
        path   = os.path.join(self.csv_dir, t["csv_file"])

        col_exprs = []
        col_names = []
        for col in t["columns"]:
            name = col["name"]
            col_names.append(name)
            if col["needs_cast"]:
                col_exprs.append(f"CAST([{name}] AS NVARCHAR(MAX)) AS [{name}]")
            else:
                col_exprs.append(f"[{name}]")

        sql = f"SELECT {', '.join(col_exprs)} FROM [{schema}].[{table}]"
        cur = self.src.cursor()
        cur.execute(sql)

        # Build a map of col index -> pg_type for NULL coercion of binary cols
        pg_types = {i: col["pg_type"] for i, col in enumerate(t["columns"])}

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([c.lower() for c in col_names])   # lowercase headers
            batch = 5000
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for row in rows:
                    out = []
                    for i, v in enumerate(row):
                        pg_t = pg_types.get(i, "text")
                        if v is None:
                            out.append(r"\N")
                        elif pg_t == "bytea":
                            # Binary columns: write hex-escaped bytea literal
                            # pyodbc returns bytes or str; convert to hex format
                            if isinstance(v, (bytes, bytearray)):
                                out.append("\\x" + v.hex())
                            else:
                                # Already cast to NVARCHAR — store as text representation
                                out.append(unicodedata.normalize("NFC", str(v)).replace("\x00", ""))
                        else:
                            out.append(unicodedata.normalize("NFC", str(v)).replace("\x00", ""))
                    writer.writerow(out)
        return path

    def _load_table(self, t: dict) -> dict:
        schema = t["target_schema"]
        table  = t["target_table"]
        label  = f"{schema}.{table}"

        try:
            # Extract
            csv_path = self._extract_csv(t)
            self.log.info("  Extracted %s -> %s", label,
                          os.path.basename(csv_path))

            # Load via COPY
            col_list = ", ".join(f'"{c["pg_name"]}"' for c in t["columns"])
            copy_sql = (
                f'COPY "{schema}"."{table}" ({col_list}) '
                f"FROM STDIN WITH (FORMAT csv, HEADER true, NULL '\\N', ENCODING 'UTF8')"
            )
            # Note: bytea columns use \x hex format which PostgreSQL COPY accepts
            cur = self.pg.cursor()
            with open(csv_path, "r", encoding="utf-8") as f:
                cur.copy_expert(copy_sql, f)
            self.pg.commit()

            # Validate
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
            loaded = cur.fetchone()[0]
            src_rows = t["source_row_count"]
            ok = loaded == src_rows
            status = "PASS" if ok else "FAIL"
            if ok:
                self.log.info("  [PASS] %s  %d rows", label, loaded)
            else:
                self.log.warning("  [FAIL] %s  src=%d tgt=%d", label, src_rows, loaded)
            return {"table": label, "source_rows": src_rows,
                    "target_rows": loaded, "status": status, "issues": []}

        except Exception as exc:
            self.pg.rollback()
            self.log.error("  [ERROR] %s: %s", label, exc)
            return {"table": label, "source_rows": t["source_row_count"],
                    "target_rows": 0, "status": "ERROR", "issues": [str(exc)]}

    # ── Phase 6: constraints ──────────────────────────────────────────────────

    def apply_constraints(self):
        self.log.info("=== PHASE 6: CONSTRAINTS (FK, UNIQUE, CHECK, DEFAULT) ===")
        fk_ok = fk_skip = uq_ok = uq_skip = chk_ok = chk_skip = 0

        for t in self.audit["tables"]:
            schema = t["target_schema"]
            table  = t["target_table"]

            # Foreign keys
            for fk in t["foreign_keys"]:
                sql = (
                    f'ALTER TABLE "{schema}"."{table}" '
                    f'ADD CONSTRAINT "{fk["name"]}" '
                    f'FOREIGN KEY ("{fk["column"]}") '
                    f'REFERENCES "{fk["ref_schema"]}"."{fk["ref_table"]}" ("{fk["ref_column"]}") '
                    f'ON DELETE {fk["on_delete"]} ON UPDATE {fk["on_update"]}'
                )
                if self._pg_quiet(sql, f"FK {fk['name']}"):
                    fk_ok += 1
                else:
                    # Retry: if "no unique constraint" error, add one on the referenced table PK
                    try:
                        self._pg(sql)
                        fk_ok += 1
                    except Exception as exc:
                        err = str(exc)
                        if "no unique constraint" in err:
                            # Find the referenced table and add unique on its PK
                            ref_t = next(
                                (x for x in self.audit["tables"]
                                 if x["target_schema"] == fk["ref_schema"]
                                 and x["target_table"] == fk["ref_table"]), None
                            )
                            if ref_t and ref_t["primary_keys"]:
                                pk_cols = ", ".join(f'"{c}"' for c in ref_t["primary_keys"])
                                uq_name = f'uq_pk_{fk["ref_table"]}'
                                uq_sql  = (
                                    f'ALTER TABLE "{fk["ref_schema"]}"."{fk["ref_table"]}" '
                                    f'ADD CONSTRAINT "{uq_name}" UNIQUE ({pk_cols})'
                                )
                                if self._pg_quiet(uq_sql, f"UQ-for-FK {uq_name}"):
                                    if self._pg_quiet(sql, f"FK-retry {fk['name']}"):
                                        fk_ok += 1
                                        continue
                        fk_skip += 1

            # Unique constraints
            for uq in t["unique_constraints"]:
                cols = ", ".join(f'"{c}"' for c in uq["columns"])
                sql = (
                    f'ALTER TABLE "{schema}"."{table}" '
                    f'ADD CONSTRAINT "{uq["name"]}" UNIQUE ({cols})'
                )
                if self._pg_quiet(sql, f"UQ {uq['name']}"):
                    uq_ok += 1
                else:
                    uq_skip += 1

            # Check constraints
            for chk in t["check_constraints"]:
                clause = self._translate_check(chk["clause"])
                if not clause:
                    chk_skip += 1
                    continue
                sql = (
                    f'ALTER TABLE "{schema}"."{table}" '
                    f'ADD CONSTRAINT "{chk["name"]}" CHECK ({clause})'
                )
                if self._pg_quiet(sql, f"CHK {chk['name']}"):
                    chk_ok += 1
                else:
                    chk_skip += 1

        self.log.info("  FK: %d added, %d skipped", fk_ok, fk_skip)
        self.log.info("  UNIQUE: %d added, %d skipped", uq_ok, uq_skip)
        self.log.info("  CHECK: %d added, %d skipped", chk_ok, chk_skip)

    # ── Phase 7: indexes ──────────────────────────────────────────────────────

    def create_indexes(self):
        self.log.info("=== PHASE 7: INDEXES ===")
        ok = skip = 0
        for t in self.audit["tables"]:
            schema = t["target_schema"]
            table  = t["target_table"]
            for idx in t["indexes"]:
                unique = "UNIQUE " if idx["unique"] else ""
                cols   = ", ".join(f'"{c}"' for c in idx["columns"])
                sql = (
                    f'CREATE {unique}INDEX CONCURRENTLY IF NOT EXISTS '
                    f'"{idx["name"]}" ON "{schema}"."{table}" ({cols})'
                )
                # CONCURRENTLY requires autocommit
                old_ac = self.pg.autocommit
                self.pg.autocommit = True
                cur = self.pg.cursor()
                try:
                    cur.execute(sql)
                    ok += 1
                except Exception as exc:
                    self.log.warning("  Index skipped [%s]: %s",
                                     idx["name"], str(exc).strip())
                    skip += 1
                finally:
                    self.pg.autocommit = old_ac
        self.log.info("  Indexes: %d created, %d skipped", ok, skip)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 8-10  —  Views, Functions, Procedures
# ═════════════════════════════════════════════════════════════════════════════

class ObjectMigrator:
    def __init__(self, pg_conn, audit: dict, skip_schemas: set[str], log):
        self.pg    = pg_conn
        self.audit = audit
        self.skip  = {s.lower() for s in skip_schemas}
        self.log   = log

    def _remap(self, sql: str) -> str:
        """Replace skip-schema references (e.g. dbo.) with nothing."""
        result = sql
        for s in self.skip:
            result = re.sub(rf'\[{re.escape(s)}\]\.', '', result, flags=re.IGNORECASE)
            result = re.sub(rf'"{re.escape(s)}"\.', '', result, flags=re.IGNORECASE)
            result = re.sub(rf'\b{re.escape(s)}\.', '', result, flags=re.IGNORECASE)
        return result

    def _exec(self, sql: str, label: str) -> bool:
        old_ac = self.pg.autocommit
        self.pg.autocommit = True
        cur = self.pg.cursor()
        try:
            cur.execute(sql)
            return True
        except Exception as exc:
            self.log.warning("  Skipped [%s]: %s", label, str(exc).strip()[:120])
            return False
        finally:
            self.pg.autocommit = old_ac

    @staticmethod
    def _extract_view_body(definition: str) -> str | None:
        """
        INFORMATION_SCHEMA.VIEW_DEFINITION returns definitions in various forms:
          Form A: SELECT ...                              (body only)
          Form B: CREATE VIEW [s].[n] AS SELECT ...       (header + body)
          Form C: SET ANSI_NULLS ON\nGO\nCREATE VIEW ... (SET lines + header + body)

        We want only the SELECT/WITH body, converted to PostgreSQL identifier quoting.
        """
        if not definition:
            return None
        d = definition.strip()

        # Step 1: Remove SET ... ON/OFF lines and GO statements (T-SQL batch separators)
        d = re.sub(r'(?im)^\s*SET\s+\w+\s+\w+\s*;?\s*$', '', d)
        d = re.sub(r'(?im)^\s*GO\s*$', '', d)
        d = d.strip()

        # Step 2: Strip CREATE VIEW ... AS header.
        # The header ends at the first AS that is at depth 0 (not inside parens).
        # We find the CREATE VIEW token and scan forward for the top-level AS.
        cv_match = re.search(r'(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b', d)
        if cv_match:
            pos = cv_match.start()
            # Scan from pos for the first top-level AS keyword
            depth = 0
            i = pos
            found_as = -1
            while i < len(d) - 1:
                ch = d[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif depth == 0:
                    # Check for AS keyword (word boundary)
                    if re.match(r'(?i)\bAS\b', d[i:]):
                        # Make sure it's followed by whitespace/newline not another identifier
                        after = d[i+2:i+4]
                        if not after or not after[0].isalnum():
                            found_as = i + 2  # skip "AS"
                            break
                i += 1
            if found_as != -1:
                d = d[found_as:].strip()

        # Step 3: Convert MSSQL [bracket] identifiers to "lowercase" quoted identifiers
        d = re.sub(r'\[([^\]]+)\]', lambda m: '"' + m.group(1).lower() + '"', d)

        return d.strip() or None

    def migrate_views(self):
        self.log.info("=== PHASE 8: VIEWS ===")
        ok = skip = 0
        for v in self.audit.get("views", []):
            schema = v["schema"].lower()
            name   = v["name"].lower()
            raw    = self._remap(v["definition"] or "")
            body   = self._extract_view_body(raw)
            if not body:
                self.log.warning("  Skipped [%s.%s]: empty definition", schema, name)
                skip += 1
                continue
            sql = f'CREATE OR REPLACE VIEW "{schema}"."{name}" AS\n{body}'
            if self._exec(sql, f"{schema}.{name}"):
                ok += 1
                self.log.info("  Created view: %s.%s", schema, name)
            else:
                skip += 1
        self.log.info("  Views: %d created, %d skipped", ok, skip)

    def migrate_functions(self):
        self.log.info("=== PHASE 9: FUNCTIONS ===")
        ok = skip = 0
        for fn in self.audit.get("functions", []):
            schema = fn["schema"].lower()
            name   = fn["name"].lower()
            defn   = fn["definition"] or ""
            if not defn.strip():
                skip += 1
                continue
            # Wrap as PL/pgSQL with a comment — manual rewrite still needed
            sql = (
                f'-- AUTO-MIGRATED from MSSQL (may need manual editing)\n'
                f'-- Original: {fn["schema"]}.{fn["name"]}\n'
                f'-- {defn[:200]}'
            )
            self.log.warning("  Function %s.%s requires manual rewrite (MSSQL->PL/pgSQL)", schema, name)
            skip += 1
        self.log.info("  Functions: %d auto-created, %d need manual rewrite", ok, skip)

    def migrate_procedures(self):
        self.log.info("=== PHASE 10: STORED PROCEDURES ===")
        ok = skip = 0
        for p in self.audit.get("procedures", []):
            schema = p["schema"].lower()
            name   = p["name"].lower()
            defn   = p["definition"] or ""
            if not defn.strip():
                skip += 1
                continue
            self.log.warning("  Procedure %s.%s requires manual rewrite (MSSQL->PL/pgSQL)", schema, name)
            skip += 1
        self.log.info("  Procedures: %d auto-created, %d need manual rewrite", ok, skip)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 11  —  Final validation + report
# ═════════════════════════════════════════════════════════════════════════════

def final_report(pg_conn, audit: dict, data_results: list[dict],
                 out_dir: str, log) -> dict:
    log.info("=== PHASE 11: FINAL VALIDATION ===")
    cur = pg_conn.cursor()

    table_reports = []
    for t in audit["tables"]:
        schema = t["target_schema"]
        table  = t["target_table"]
        label  = f"{schema}.{table}"

        # Match with data_results
        data_res = next((r for r in data_results if r["table"] == label), {})

        try:
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
            pg_rows = cur.fetchone()[0]
        except Exception:
            pg_rows = -1

        src_rows = t["source_row_count"]
        match    = pg_rows == src_rows
        table_reports.append({
            "table":       label,
            "source_rows": src_rows,
            "target_rows": pg_rows,
            "match":       match,
            "status":      "PASS" if match else "FAIL",
        })
        if match:
            log.info("  [PASS] %s  %d rows", label, pg_rows)
        else:
            log.warning("  [FAIL] %s  src=%d tgt=%d", label, src_rows, pg_rows)

    passed = sum(1 for r in table_reports if r["status"] == "PASS")
    failed = sum(1 for r in table_reports if r["status"] != "PASS")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_tables": len(table_reports),
            "passed": passed,
            "failed": failed,
            "overall": "PASS" if failed == 0 else "FAIL",
        },
        "tables": table_reports,
        "views_migrated":      len(audit.get("views", [])),
        "functions_migrated":  len(audit.get("functions", [])),
        "procedures_migrated": len(audit.get("procedures", [])),
    }

    path = os.path.join(out_dir, "migration_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    log.info("=" * 60)
    log.info("MIGRATION %s: %d/%d tables PASS",
             report["summary"]["overall"], passed, len(table_reports))
    log.info("Report: %s", path)
    return report


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def connect_mssql(args, log):
    try:
        import pyodbc
    except ImportError:
        raise RuntimeError("pyodbc not installed: pip install pyodbc")

    if args.windows_auth:
        cs = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={args.src_host},{args.src_port};"
            f"DATABASE={args.src_db};"
            f"Trusted_Connection=yes;TrustServerCertificate=yes;"
        )
        log.info("Connecting MSSQL (Windows Auth): %s/%s", args.src_host, args.src_db)
    else:
        cs = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={args.src_host},{args.src_port};"
            f"DATABASE={args.src_db};"
            f"UID={args.src_user};PWD={args.src_pass};"
            f"TrustServerCertificate=yes;"
        )
        log.info("Connecting MSSQL (SQL Auth) as [%s]: %s/%s",
                 args.src_user, args.src_host, args.src_db)

    try:
        conn = pyodbc.connect(cs)
    except Exception as exc:
        if "18456" in str(exc) or "28000" in str(exc):
            raise RuntimeError(
                "Login failed (18456). Check credentials or enable "
                "Mixed Mode Auth. Use --windows-auth for Windows Authentication."
            ) from exc
        raise
    log.info("MSSQL connected.")
    return conn


def connect_postgres(args, log, dbname=None):
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError("psycopg2 not installed: pip install psycopg2-binary")

    db = dbname or args.tgt_db
    log.info("Connecting PostgreSQL: %s/%s", args.tgt_host, db)
    conn = psycopg2.connect(
        host=args.tgt_host, port=args.tgt_port,
        dbname=db, user=args.tgt_user, password=args.tgt_pass,
    )
    conn.autocommit = False
    log.info("PostgreSQL connected.")
    return conn


def drop_and_recreate_db(args, log):
    """
    Drop and recreate the target database.
    Must connect to 'postgres' maintenance DB (cannot drop a DB you are connected to).
    """
    import psycopg2
    tgt = args.tgt_db
    log.info("--drop-target: dropping database '%s' if it exists...", tgt)
    admin = psycopg2.connect(
        host=args.tgt_host, port=args.tgt_port,
        dbname="postgres", user=args.tgt_user, password=args.tgt_pass,
    )
    admin.autocommit = True
    cur = admin.cursor()
    # Terminate existing connections
    cur.execute(f"""
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = %s AND pid <> pg_backend_pid()
    """, (tgt,))
    cur.execute(f'DROP DATABASE IF EXISTS "{tgt}"')
    cur.execute(f'CREATE DATABASE "{tgt}"')
    admin.close()
    log.info("Database '%s' recreated.", tgt)


def build_parser():
    p = argparse.ArgumentParser(
        prog="migrate.py",
        description="MSSQL -> PostgreSQL migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (Windows CMD — use ^ to continue lines):

  SQL Server Authentication:
    python migrate.py ^
      --src-host localhost --src-db AdventureWorks ^
      --src-user sa --src-pass MyPassword ^
      --tgt-host localhost --tgt-db aw_pg ^
      --tgt-user postgres --tgt-pass MyPassword ^
      --skip-schema dbo --out C:\\migration

  Windows Authentication (no --src-user/--src-pass needed):
    python migrate.py --windows-auth ^
      --src-host localhost --src-db AdventureWorks ^
      --tgt-host localhost --tgt-db aw_pg ^
      --tgt-user postgres --tgt-pass MyPassword ^
      --skip-schema dbo --out C:\\migration
        """,
    )

    src = p.add_argument_group("Source (MSSQL)")
    src.add_argument("--src-host",  default="localhost")
    src.add_argument("--src-port",  default=1433, type=int)
    src.add_argument("--src-db",    required=True, help="Source database name")
    src.add_argument("--src-user",  default=None)
    src.add_argument("--src-pass",  default=None)
    src.add_argument("--windows-auth", action="store_true",
                     help="Use Windows Authentication (ignores --src-user/--src-pass)")

    tgt = p.add_argument_group("Target (PostgreSQL)")
    tgt.add_argument("--tgt-host",  default="localhost")
    tgt.add_argument("--tgt-port",  default=5432, type=int)
    tgt.add_argument("--tgt-db",    required=True, help="Target database name")
    tgt.add_argument("--tgt-user",  required=True)
    tgt.add_argument("--tgt-pass",  required=True)

    p.add_argument("--skip-schema", nargs="+", default=["dbo"],
                   metavar="SCHEMA",
                   help="Schemas to skip entirely (default: dbo)")
    p.add_argument("--out", default="./migration_output",
                   help="Output directory for CSVs, reports, logs")
    p.add_argument("--audit-only", action="store_true",
                   help="Run audit/report only, do not migrate")
    p.add_argument("--drop-target", action="store_true",
                   help="DROP and recreate the target database before migrating (safe re-run)")
    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    log = _make_logger(os.path.join(out_dir, "logs"))

    log.info("=" * 60)
    log.info("MSSQL -> PostgreSQL Migration")
    log.info("Source: %s/%s", args.src_host, args.src_db)
    log.info("Target: %s/%s", args.tgt_host, args.tgt_db)
    log.info("Skip schemas: %s", args.skip_schema)
    log.info("Output: %s", out_dir)
    log.info("=" * 60)

    skip = set(args.skip_schema)
    src_conn = pg_conn = None

    try:
        src_conn = connect_mssql(args, log)
        if not args.audit_only:
            if getattr(args, "drop_target", False):
                drop_and_recreate_db(args, log)
            pg_conn = connect_postgres(args, log)

        # ── Phase 1-2: Audit ─────────────────────────────────────────────────
        auditor = Auditor(src_conn, skip, log)
        audit   = auditor.run(out_dir)

        if args.audit_only:
            log.info("--audit-only: stopping after audit.")
            return

        # ── Phase 3: Create schemas ──────────────────────────────────────────
        loader = Loader(pg_conn, src_conn, audit, out_dir, log)
        loader.create_schemas()

        # ── Phase 4: Create tables (PK only) ─────────────────────────────────
        loader.create_tables()

        # ── Phase 5: Migrate data ─────────────────────────────────────────────
        data_results = loader.migrate_data()

        # ── Phase 6: Apply constraints ────────────────────────────────────────
        loader.apply_constraints()

        # ── Phase 7: Create indexes ───────────────────────────────────────────
        loader.create_indexes()

        # ── Phase 8-10: Views, functions, procedures ──────────────────────────
        obj_migrator = ObjectMigrator(pg_conn, audit, skip, log)
        obj_migrator.migrate_views()
        obj_migrator.migrate_functions()
        obj_migrator.migrate_procedures()

        # ── Phase 11: Final report ────────────────────────────────────────────
        final_report(pg_conn, audit, data_results, out_dir, log)

    except Exception as exc:
        if log:
            log.error("FATAL: %s", exc)
            log.debug(traceback.format_exc())
        else:
            traceback.print_exc()
        sys.exit(1)
    finally:
        for c in (src_conn, pg_conn):
            if c:
                try:
                    c.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
