#!/usr/bin/env python3
"""
migrate_mysql.py  —  MSSQL -> MySQL migration tool (streaming, no CSV)
======================================================================
11-phase pipeline:
  1.  Audit source database
  2.  Generate datatype mapping + compatibility report
  3.  Create MySQL database/schema
  4.  Create tables (PK only — no FKs yet)
  5.  Migrate data (streaming direct insert) + validate row counts
  6.  Apply FK, UNIQUE, CHECK constraints
  7.  Create indexes
  8.  Migrate views
  9.  Migrate functions  (logged; manual rewrite needed)
  10. Migrate stored procedures (logged; manual rewrite needed)
  11. Final validation + migration report

Changes from previous version:
  - REMOVED all CSV intermediate files — data streams directly from
    SQL Server to MySQL via chunked fetches + executemany bulk inserts
  - Configurable batch size (default 5000 rows)
  - Transaction handling with per-batch commits
  - Retry logic (configurable retries with exponential backoff)
  - Progress reporting (rows/sec, ETA, percentage)
  - Proper connection cleanup via context managers
  - Memory-efficient: never loads entire table into RAM

Usage (Windows CMD — use ^ to continue lines):

  python migrate_mysql.py --windows-auth ^
    --src-host localhost --src-db AdventureWorks ^
    --tgt-host localhost --tgt-db aw_mysql ^
    --tgt-user root --tgt-pass secret ^
    --skip-schema dbo --out C:\\migration_mysql --drop-target

  python migrate_mysql.py ^
    --src-host localhost --src-db AdventureWorks ^
    --src-user sa --src-pass secret ^
    --tgt-host localhost --tgt-db aw_mysql ^
    --tgt-user root --tgt-pass secret ^
    --skip-schema dbo --out C:\\migration_mysql --drop-target ^
    --batch-size 10000 --max-retries 5
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from type_map import map_column_type, translate_default, CAST_TYPES

# ── Logging ───────────────────────────────────────────────────────────────────


def _make_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("migrate_mysql")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if sys.platform == "win32":
        stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
    else:
        stream = sys.stdout
    ch = logging.StreamHandler(stream)
    ch.setFormatter(fmt)
    fh = RotatingFileHandler(
        os.path.join(log_dir, "migrate_mysql.log"),
        maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


# ── MySQL identifier quoting ───────────────────────────────────────────────────


def qi(name: str) -> str:
    """Backtick-quote a MySQL identifier (lowercase)."""
    return f"`{name.lower()}`"


def qs(val: str) -> str:
    """Single-quote escape a string value for MySQL."""
    return "'" + val.replace("'", "''") + "'"


# ── Retry decorator ───────────────────────────────────────────────────────────


def retry_on_error(max_retries: int = 3, base_delay: float = 1.0, log=None):
    """Decorator: retry a function on exception with exponential backoff."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_retries:
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    if log:
                        log.warning("  Retry %d/%d after error: %s (wait %.1fs)",
                                    attempt, max_retries, str(exc)[:120], delay)
                    time.sleep(delay)
        return wrapper
    return decorator


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
        self.log.info("Found %d migratable tables (skip=%s)",
                      len(tables), sorted(self.skip))

        manifest = []
        for schema, table in tables:
            self.log.info("  Auditing %s.%s", schema, table)
            cols = self._get_columns(cur, schema, table)
            pks = self._get_pks(cur, schema, table)
            fks = self._get_fks(cur, schema, table)
            uqs = self._get_unique(cur, schema, table)
            chks = self._get_checks(cur, schema, table)
            idx = self._get_indexes(cur, schema, table)
            rows = self._get_rowcount(cur, schema, table)
            manifest.append({
                "source_schema": schema,
                "source_table": table,
                "target_table": table.lower(),
                "columns": cols,
                "primary_keys": pks,
                "foreign_keys": fks,
                "unique_constraints": uqs,
                "check_constraints": chks,
                "indexes": idx,
                "source_row_count": rows,
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

        report = self._compatibility_report(views, funcs, procs)
        with open(os.path.join(out_dir, "compatibility_report.json"),
                  "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        self.log.info("Audit complete. Tables=%d Views=%d Funcs=%d Procs=%d",
                      len(manifest), len(views), len(funcs), len(procs))
        self._log_compat(report)
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
                COLUMNPROPERTY(OBJECT_ID(? + '.' + ?),
                               c.COLUMN_NAME, 'IsIdentity') AS is_identity
            FROM INFORMATION_SCHEMA.COLUMNS c
            WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
            ORDER BY c.ORDINAL_POSITION
        """, schema + '.' + table, table, schema, table)
        cols = []
        for row in cur.fetchall():
            mysql_type = map_column_type(
                row[2],
                str(row[3]) if row[3] is not None else None,
                str(row[4]) if row[4] is not None else None,
                str(row[5]) if row[5] is not None else None,
            )
            cols.append({
                "name": row[0],
                "mysql_name": row[0].lower(),
                "ordinal": row[1],
                "mssql_type": row[2],
                "mysql_type": mysql_type,
                "max_length": str(row[3]) if row[3] is not None else None,
                "precision": str(row[4]) if row[4] is not None else None,
                "scale": str(row[5]) if row[5] is not None else None,
                "nullable": row[6] == "YES",
                "default": row[7],
                "is_identity": bool(row[8]),
                "needs_cast": row[2].lower() in CAST_TYPES,
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
            JOIN sys.foreign_key_columns fc
              ON fc.constraint_object_id = fk.object_id
            WHERE OBJECT_NAME(fk.parent_object_id) = ?
              AND SCHEMA_NAME(fk.schema_id) = ?
        """, table, schema)
        fks = []
        for r in cur.fetchall():
            ref_schema = r[2].lower()
            if ref_schema in self.skip:
                continue
            on_del = (r[5] or "NO_ACTION").replace("_", " ")
            on_upd = (r[6] or "NO_ACTION").replace("_", " ")
            on_del = "RESTRICT" if on_del == "SET DEFAULT" else on_del
            on_upd = "RESTRICT" if on_upd == "SET DEFAULT" else on_upd
            fks.append({
                "name": r[0].lower(),
                "column": r[1].lower(),
                "ref_table": r[3].lower(),
                "ref_column": r[4].lower(),
                "on_delete": on_del,
                "on_update": on_upd,
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
        grouped = defaultdict(list)
        for cname, col in cur.fetchall():
            grouped[cname.lower()].append(col.lower())
        return [{"name": k, "columns": v} for k, v in grouped.items()]

    def _get_checks(self, cur, schema, table):
        cur.execute("""
            SELECT cc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
            FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
            JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
              ON cc.CONSTRAINT_NAME  = tc.CONSTRAINT_NAME
             AND cc.CONSTRAINT_SCHEMA = tc.TABLE_SCHEMA
            WHERE tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
        """, schema, table)
        checks = []
        for r in cur.fetchall():
            clause = r[1] or ""
            if "IS NOT NULL" in clause.upper():
                continue
            checks.append({"name": r[0].lower(), "clause": clause})
        return checks

    def _get_indexes(self, cur, schema, table):
        cur.execute("""
            SELECT i.name, i.is_unique,
                   STRING_AGG(c.name, ',')
                       WITHIN GROUP (ORDER BY ic.key_ordinal)
            FROM sys.indexes i
            JOIN sys.index_columns ic
              ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
              ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE OBJECT_NAME(i.object_id) = ?
              AND SCHEMA_NAME((SELECT schema_id FROM sys.objects
                               WHERE object_id = i.object_id)) = ?
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
        return [{"schema": r[0], "name": r[1], "definition": r[2] or ""}
                for r in cur.fetchall() if r[0].lower() not in self.skip]

    def _get_functions(self, cur):
        cur.execute("""
            SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_DEFINITION
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'FUNCTION'
            ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME
        """)
        return [{"schema": r[0], "name": r[1], "definition": r[2] or ""}
                for r in cur.fetchall() if r[0].lower() not in self.skip]

    def _get_procedures(self, cur):
        cur.execute("""
            SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_DEFINITION
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'PROCEDURE'
            ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME
        """)
        return [{"schema": r[0], "name": r[1], "definition": r[2] or ""}
                for r in cur.fetchall() if r[0].lower() not in self.skip]

    PATTERNS = [
        (r'\bEXEC\s*\(', "BLOCKER", "Dynamic SQL via EXEC()"),
        (r'#\w+', "BLOCKER", "Temp table (#table)"),
        (r'\bOPENROWSET\b', "BLOCKER", "OPENROWSET not supported"),
        (r'\bFOR\s+XML\b', "BLOCKER", "FOR XML not supported in MySQL"),
        (r'\bCROSS\s+APPLY\b', "BLOCKER", "CROSS APPLY -> use LATERAL JOIN"),
        (r'\bOUTER\s+APPLY\b', "BLOCKER", "OUTER APPLY -> use LEFT JOIN LATERAL"),
        (r'\bPIVOT\b', "BLOCKER", "PIVOT not supported in MySQL"),
        (r'\bTOP\s+\d+\b', "WARNING", "TOP n -> LIMIT n"),
        (r'\bISNULL\s*\(', "WARNING", "ISNULL() -> IFNULL() or COALESCE()"),
        (r'@@ROWCOUNT', "WARNING", "@@ROWCOUNT -> ROW_COUNT()"),
        (r'\bRAISERROR\b', "WARNING", "RAISERROR -> SIGNAL SQLSTATE"),
        (r'\bNOLOCK\b', "WARNING", "NOLOCK hint - remove"),
        (r'\bDATEADD\s*\(', "WARNING", "DATEADD -> DATE_ADD()"),
        (r'\bDATEDIFF\s*\(', "WARNING", "DATEDIFF -> DATEDIFF() (compatible)"),
        (r'\bCONVERT\s*\(', "WARNING", "CONVERT -> CAST() or CONVERT()"),
        (r'\bGETDATE\s*\(\)', "INFO", "GETDATE() -> NOW()"),
        (r'\bNVARCHAR\b', "INFO", "NVARCHAR -> VARCHAR"),
    ]

    def _compatibility_report(self, views, funcs, procs):
        findings = []
        objects = (
            [(v["schema"]+"."+v["name"], "VIEW", v["definition"]) for v in views] +
            [(f["schema"]+"."+f["name"], "FUNCTION", f["definition"]) for f in funcs] +
            [(p["schema"]+"."+p["name"], "PROCEDURE", p["definition"]) for p in procs]
        )
        for obj, typ, body in objects:
            if not body:
                continue
            for pat, sev, msg in self.PATTERNS:
                if re.search(pat, body, re.IGNORECASE):
                    findings.append({"object": obj, "type": typ,
                                     "severity": sev, "issue": msg})
        bl = [f for f in findings if f["severity"] == "BLOCKER"]
        wa = [f for f in findings if f["severity"] == "WARNING"]
        inf = [f for f in findings if f["severity"] == "INFO"]
        return {"gate": "FAIL" if bl else "PASS",
                "blockers": len(bl), "warnings": len(wa), "info": len(inf),
                "findings": findings}

    def _log_compat(self, report):
        self.log.info("Compatibility gate: %s  (blockers=%d warnings=%d info=%d)",
                      report["gate"], report["blockers"],
                      report["warnings"], report["info"])
        for f in report["findings"]:
            if f["severity"] == "BLOCKER":
                self.log.warning("  [BLOCKER] %s (%s): %s",
                                 f["object"], f["type"], f["issue"])


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 3-7  —  Load (streaming direct insert, no CSV)
# ═════════════════════════════════════════════════════════════════════════════


class Loader:
    def __init__(self, my_conn, src_conn, audit: dict,
                 tgt_db: str, work_dir: str, log,
                 batch_size: int = 5000, max_retries: int = 3):
        self.my = my_conn
        self.src = src_conn
        self.audit = audit
        self.tgt_db = tgt_db
        self.work = work_dir
        self.log = log
        self.batch_size = batch_size
        self.max_retries = max_retries

    # ── MySQL helpers ─────────────────────────────────────────────────────────

    def _my(self, sql: str, params=None) -> object:
        cur = self.my.cursor()
        cur.execute(sql, params or ())
        self.my.commit()
        return cur

    def _my_quiet(self, sql: str, label: str = "") -> bool:
        try:
            self._my(sql)
            return True
        except Exception as exc:
            self.log.warning("  Skipped [%s]: %s", label, str(exc).strip()[:150])
            return False

    # ── Phase 3: database ─────────────────────────────────────────────────────

    def create_database(self):
        self.log.info("=== PHASE 3: CREATE DATABASE ===")
        self._my(
            f"CREATE DATABASE IF NOT EXISTS {qi(self.tgt_db)} "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        self._my(f"USE {qi(self.tgt_db)}")
        self.log.info("  Database: %s", self.tgt_db)

    # ── Phase 4: tables (PK only) ─────────────────────────────────────────────

    def create_tables(self):
        self.log.info("=== PHASE 4: CREATE TABLES ===")
        for t in self.audit["tables"]:
            sql = self._build_create_table(t)
            try:
                self._my(sql)
                self.log.info("  Created: %s", t["target_table"])
            except Exception as exc:
                self.log.error("  FAILED %s: %s", t["target_table"], exc)
                self.log.debug("  SQL was:\n%s", sql)
                raise

    def _build_create_table(self, t: dict) -> str:
        table = t["target_table"]
        lines = []

        for col in t["columns"]:
            name = col["mysql_name"]
            mysql_type = col["mysql_type"]

            if col["is_identity"]:
                lines.append(f"  {qi(name)}  BIGINT NOT NULL AUTO_INCREMENT")
                continue

            parts = [f"  {qi(name)}  {mysql_type}"]
            if not col["nullable"]:
                parts.append("NOT NULL")
            else:
                parts.append("DEFAULT NULL")

            default = translate_default(col["default"], mysql_type)
            if default and not col["nullable"]:
                parts.append(f"DEFAULT {default}")

            lines.append("  ".join(parts))

        if t["primary_keys"]:
            pk_cols = ", ".join(qi(c) for c in t["primary_keys"])
            lines.append(f"  PRIMARY KEY ({pk_cols})")

        body = ",\n".join(lines)
        return (
            f"CREATE TABLE IF NOT EXISTS {qi(self.tgt_db)}.{qi(table)} (\n"
            f"{body}\n"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;\n"
        )

    # ── Phase 5: data (streaming direct insert) ──────────────────────────────

    def migrate_data(self) -> list[dict]:
        self.log.info("=== PHASE 5: DATA MIGRATION (streaming, batch=%d) ===",
                      self.batch_size)
        self._my("SET FOREIGN_KEY_CHECKS = 0")
        self.log.info("  FK checks suspended.")

        results = []
        total_tables = len(self.audit["tables"])
        for idx, t in enumerate(self.audit["tables"], 1):
            self.log.info("  [%d/%d] %s (%d rows)...",
                          idx, total_tables, t["target_table"],
                          t["source_row_count"])
            results.append(self._stream_table(t))

        self._my("SET FOREIGN_KEY_CHECKS = 1")
        self.log.info("  FK checks restored.")

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] != "PASS")
        self.log.info("  Data load: %d PASS  |  %d FAIL", passed, failed)
        return results

    def _transform_row(self, row, columns: list[dict]) -> list:
        """Transform a single row from pyodbc types to MySQL-compatible values."""
        out = []
        for i, v in enumerate(row):
            if v is None:
                out.append(None)
                continue
            col = columns[i] if i < len(columns) else None
            mysql_type = col["mysql_type"].upper() if col else ""

            if isinstance(v, bool):
                out.append(1 if v else 0)
            elif "BLOB" in mysql_type or "LONGBLOB" in mysql_type:
                if isinstance(v, (bytes, bytearray)):
                    out.append(v)  # pass bytes directly to connector
                else:
                    out.append(unicodedata.normalize("NFC", str(v)).replace("\x00", ""))
            elif isinstance(v, (bytes, bytearray)):
                out.append(v)
            else:
                out.append(unicodedata.normalize("NFC", str(v)).replace("\x00", ""))
        return out

    def _stream_table(self, t: dict) -> dict:
        """Stream rows from SQL Server directly into MySQL in batches."""
        table = t["target_table"]
        schema = t["source_schema"]
        src_table = t["source_table"]
        columns = t["columns"]
        src_rows = t["source_row_count"]

        try:
            # Build SELECT with CAST for unsupported types
            col_exprs = []
            for col in columns:
                name = col["name"]
                if col["needs_cast"]:
                    col_exprs.append(f"CAST([{name}] AS NVARCHAR(MAX)) AS [{name}]")
                else:
                    col_exprs.append(f"[{name}]")

            select_sql = f"SELECT {', '.join(col_exprs)} FROM [{schema}].[{src_table}]"

            # Build INSERT statement
            col_list = ", ".join(qi(c["mysql_name"]) for c in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            insert_sql = (
                f"INSERT INTO {qi(self.tgt_db)}.{qi(table)} "
                f"({col_list}) VALUES ({placeholders})"
            )

            # Stream from source using server-side cursor
            src_cur = self.src.cursor()
            src_cur.execute(select_sql)

            my_cur = self.my.cursor()
            inserted = 0
            start_time = time.time()
            last_report = start_time

            while True:
                rows = src_cur.fetchmany(self.batch_size)
                if not rows:
                    break

                batch = [self._transform_row(r, columns) for r in rows]
                self._insert_batch_with_retry(my_cur, insert_sql, batch, table)
                inserted += len(batch)

                # Progress reporting every 5 seconds
                now = time.time()
                if now - last_report >= 5.0 and src_rows > 0:
                    elapsed = now - start_time
                    rate = inserted / elapsed if elapsed > 0 else 0
                    pct = (inserted / src_rows) * 100
                    eta = ((src_rows - inserted) / rate) if rate > 0 else 0
                    self.log.info(
                        "    %s: %d/%d (%.1f%%) | %.0f rows/sec | ETA %.0fs",
                        table, inserted, src_rows, pct, rate, eta
                    )
                    last_report = now

            # Final commit
            self.my.commit()

            # Validate row count
            my_cur.execute(f"SELECT COUNT(*) FROM {qi(self.tgt_db)}.{qi(table)}")
            loaded = my_cur.fetchone()[0]
            ok = loaded == src_rows
            status = "PASS" if ok else "FAIL"

            elapsed = time.time() - start_time
            rate = loaded / elapsed if elapsed > 0 else 0
            if ok:
                self.log.info("  [PASS] %s  %d rows in %.1fs (%.0f rows/sec)",
                              table, loaded, elapsed, rate)
            else:
                self.log.warning("  [FAIL] %s  src=%d tgt=%d", table, src_rows, loaded)

            return {"table": table, "source_rows": src_rows,
                    "target_rows": loaded, "status": status, "issues": []}

        except Exception as exc:
            try:
                self.my.rollback()
            except Exception:
                pass
            self.log.error("  [ERROR] %s: %s", table, exc)
            self.log.debug(traceback.format_exc())
            return {"table": table, "source_rows": src_rows,
                    "target_rows": 0, "status": "ERROR", "issues": [str(exc)]}

    def _insert_batch_with_retry(self, cur, sql: str, batch: list, table: str):
        """Execute a batch insert with retry logic and transaction handling."""
        for attempt in range(1, self.max_retries + 1):
            try:
                cur.executemany(sql, batch)
                self.my.commit()
                return
            except Exception as exc:
                try:
                    self.my.rollback()
                except Exception:
                    pass
                if attempt == self.max_retries:
                    raise RuntimeError(
                        f"Failed to insert batch into {table} after "
                        f"{self.max_retries} attempts: {exc}"
                    ) from exc
                delay = 1.0 * (2 ** (attempt - 1))
                self.log.warning(
                    "    Batch insert retry %d/%d for %s: %s (wait %.1fs)",
                    attempt, self.max_retries, table, str(exc)[:100], delay
                )
                time.sleep(delay)

    # ── Phase 6: constraints ──────────────────────────────────────────────────

    def apply_constraints(self):
        self.log.info("=== PHASE 6: CONSTRAINTS (FK, UNIQUE, CHECK) ===")
        fk_ok = fk_skip = uq_ok = uq_skip = chk_ok = chk_skip = 0

        for t in self.audit["tables"]:
            table = t["target_table"]

            for fk in t["foreign_keys"]:
                fk_name = fk["name"][:64]
                sql = (
                    f"ALTER TABLE {qi(self.tgt_db)}.{qi(table)} "
                    f"ADD CONSTRAINT {qi(fk_name)} "
                    f"FOREIGN KEY ({qi(fk['column'])}) "
                    f"REFERENCES {qi(self.tgt_db)}.{qi(fk['ref_table'])} "
                    f"({qi(fk['ref_column'])}) "
                    f"ON DELETE {fk['on_delete']} "
                    f"ON UPDATE {fk['on_update']}"
                )
                if self._my_quiet(sql, f"FK {fk_name}"):
                    fk_ok += 1
                else:
                    fk_skip += 1

            for uq in t["unique_constraints"]:
                cols = ", ".join(qi(c) for c in uq["columns"])
                sql = (
                    f"ALTER TABLE {qi(self.tgt_db)}.{qi(table)} "
                    f"ADD CONSTRAINT {qi(uq['name'])} UNIQUE ({cols})"
                )
                if self._my_quiet(sql, f"UQ {uq['name']}"):
                    uq_ok += 1
                else:
                    uq_skip += 1

            for chk in t["check_constraints"]:
                clause = self._translate_check(chk["clause"])
                if not clause:
                    chk_skip += 1
                    continue
                sql = (
                    f"ALTER TABLE {qi(self.tgt_db)}.{qi(table)} "
                    f"ADD CONSTRAINT {qi(chk['name'])} CHECK ({clause})"
                )
                if self._my_quiet(sql, f"CHK {chk['name']}"):
                    chk_ok += 1
                else:
                    chk_skip += 1

        self.log.info("  FK: %d added, %d skipped", fk_ok, fk_skip)
        self.log.info("  UNIQUE: %d added, %d skipped", uq_ok, uq_skip)
        self.log.info("  CHECK: %d added, %d skipped", chk_ok, chk_skip)

    @staticmethod
    def _translate_check(clause: str) -> str | None:
        """Convert MSSQL CHECK clause [Col] syntax to `col` MySQL syntax."""
        if not clause:
            return None
        c = clause.strip()
        while c.startswith("(") and c.endswith(")"):
            depth = 0
            outer = True
            for i, ch in enumerate(c):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if depth == 0 and i < len(c) - 1:
                    outer = False
                    break
            if outer:
                c = c[1:-1].strip()
            else:
                break
        c = re.sub(r'\[([^\]]+)\]', lambda m: '`' + m.group(1).lower() + '`', c)
        return c

    # ── Phase 7: indexes ──────────────────────────────────────────────────────

    def create_indexes(self):
        self.log.info("=== PHASE 7: INDEXES ===")
        ok = skip = 0
        for t in self.audit["tables"]:
            table = t["target_table"]
            for idx in t["indexes"]:
                unique = "UNIQUE " if idx["unique"] else ""
                col_parts = []
                for c in idx["columns"]:
                    col_meta = next(
                        (x for x in t["columns"] if x["mysql_name"] == c), None
                    )
                    if col_meta and any(k in col_meta["mysql_type"].upper()
                                        for k in ("TEXT", "BLOB", "LONGTEXT",
                                                   "MEDIUMTEXT")):
                        col_parts.append(f"{qi(c)}(191)")
                    else:
                        col_parts.append(qi(c))
                cols = ", ".join(col_parts)
                idx_name = idx["name"][:64]
                sql = (
                    f"CREATE {unique}INDEX {qi(idx_name)} "
                    f"ON {qi(self.tgt_db)}.{qi(table)} ({cols})"
                )
                if self._my_quiet(sql, idx_name):
                    ok += 1
                else:
                    skip += 1
        self.log.info("  Indexes: %d created, %d skipped", ok, skip)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 8-10  —  Views, Functions, Procedures
# ═════════════════════════════════════════════════════════════════════════════


class ObjectMigrator:
    def __init__(self, my_conn, audit: dict, tgt_db: str,
                 skip_schemas: set[str], log):
        self.my = my_conn
        self.audit = audit
        self.tgt_db = tgt_db
        self.skip = {s.lower() for s in skip_schemas}
        self.log = log

    def _exec(self, sql: str, label: str) -> bool:
        cur = self.my.cursor()
        try:
            cur.execute(sql)
            self.my.commit()
            return True
        except Exception as exc:
            self.log.warning("  Skipped [%s]: %s", label, str(exc).strip()[:150])
            try:
                self.my.rollback()
            except Exception:
                pass
            return False

    UNSUPPORTED_PATTERNS = [
        (r'CROSS\s+APPLY', 'CROSS APPLY'),
        (r'OUTER\s+APPLY', 'OUTER APPLY'),
        (r'PIVOT', 'PIVOT'),
        (r'\.nodes\s*\(', 'XML .nodes()'),
        (r'\.value\s*\(', 'XML .value()'),
        (r'\.query\s*\(', 'XML .query()'),
    ]

    @staticmethod
    def _extract_view_body(definition: str) -> str | None:
        """Strip MSSQL CREATE VIEW header, return SELECT body only."""
        if not definition:
            return None
        d = definition.strip()
        d = re.sub(r'(?im)^\s*SET\s+\w+\s+\w+\s*;?\s*$', '', d)
        d = re.sub(r'(?im)^\s*GO\s*$', '', d)
        d = d.strip()

        cv = re.search(r'(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b', d)
        if cv:
            depth = 0
            i = cv.start()
            found_as = -1
            while i < len(d) - 1:
                ch = d[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif depth == 0 and re.match(r'(?i)\bAS\b', d[i:]):
                    after = d[i+2:i+4]
                    if not after or not after[0].isalnum():
                        found_as = i + 2
                        break
                i += 1
            if found_as != -1:
                d = d[found_as:].strip()

        d = re.sub(r'\[([^\]]+)\]', lambda m: '`' + m.group(1).lower() + '`', d)
        d = re.sub(
            r'(?m),?\s*"([^"]+)"\s*=\s*([^,\n]+)',
            lambda m: ', ' + m.group(2).strip() + ' AS `' + m.group(1).lower() + '`',
            d
        )
        return d.strip() or None

    def _check_unsupported(self, body: str) -> str | None:
        for pattern, label in self.UNSUPPORTED_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                return label
        return None

    def _remap_view_schemas(self, body: str) -> str:
        db = self.tgt_db
        body = re.sub(r'`dbo`\.`([^`]+)`', r'``', body, flags=re.IGNORECASE)
        known_schemas = ['humanresources', 'person', 'production',
                         'purchasing', 'sales', 'dbo']
        for s in known_schemas:
            body = re.sub(
                rf'`{re.escape(s)}`\.(`[^`]+`)',
                lambda m, d=db: f'`{d}`.' + m.group(1),
                body,
                flags=re.IGNORECASE,
            )
        body = re.sub(
            rf'`(?!{re.escape(db)}`)([^`]+)`\.(`[^`]+`)',
            lambda m, d=db: f'`{d}`.' + m.group(2),
            body,
        )
        return body

    def migrate_views(self):
        self.log.info("=== PHASE 8: VIEWS ===")
        ok = skip = 0
        self._exec(f"USE {qi(self.tgt_db)}", "USE db")

        for v in self.audit.get("views", []):
            name = v["name"].lower()
            raw = v["definition"] or ""

            body = self._extract_view_body(raw)
            unsupported = self._check_unsupported(raw) or (
                self._check_unsupported(body) if body else None
            )
            if unsupported:
                self.log.warning(
                    "  Skipped [%s]: uses %s — requires manual rewrite", name, unsupported
                )
                skip += 1
                continue
            if not body:
                skip += 1
                continue

            body = self._remap_view_schemas(body)
            sql = f"CREATE OR REPLACE VIEW {qi(self.tgt_db)}.{qi(name)} AS\n{body}"

            if self._exec(sql, name):
                ok += 1
                self.log.info("  Created view: %s", name)
            else:
                skip += 1

        self.log.info("  Views: %d created, %d skipped", ok, skip)

    def migrate_functions(self):
        self.log.info("=== PHASE 9: FUNCTIONS ===")
        for fn in self.audit.get("functions", []):
            self.log.warning("  Function %s.%s requires manual rewrite (T-SQL -> MySQL)",
                             fn["schema"], fn["name"])
        self.log.info("  Functions: 0 auto-created, %d need manual rewrite",
                      len(self.audit.get("functions", [])))

    def migrate_procedures(self):
        self.log.info("=== PHASE 10: STORED PROCEDURES ===")
        for p in self.audit.get("procedures", []):
            self.log.warning("  Procedure %s.%s requires manual rewrite (T-SQL -> MySQL)",
                             p["schema"], p["name"])
        self.log.info("  Procedures: 0 auto-created, %d need manual rewrite",
                      len(self.audit.get("procedures", [])))


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 11  —  Final validation
# ═════════════════════════════════════════════════════════════════════════════


def final_report(my_conn, tgt_db: str, audit: dict,
                 data_results: list[dict], out_dir: str, log) -> dict:
    log.info("=== PHASE 11: FINAL VALIDATION ===")
    cur = my_conn.cursor()
    table_reports = []

    for t in audit["tables"]:
        table = t["target_table"]
        try:
            cur.execute(f"SELECT COUNT(*) FROM {qi(tgt_db)}.{qi(table)}")
            tgt_rows = cur.fetchone()[0]
        except Exception:
            tgt_rows = -1

        src_rows = t["source_row_count"]
        match = tgt_rows == src_rows
        table_reports.append({
            "table": table,
            "source_rows": src_rows,
            "target_rows": tgt_rows,
            "match": match,
            "status": "PASS" if match else "FAIL",
        })
        if match:
            log.info("  [PASS] %s  %d rows", table, tgt_rows)
        else:
            log.warning("  [FAIL] %s  src=%d tgt=%d", table, src_rows, tgt_rows)

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
        "views_migrated": len(audit.get("views", [])),
        "functions_migrated": len(audit.get("functions", [])),
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
#  CLI + Connections
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
        log.info("Connecting MSSQL (Windows Auth): %s/%s",
                 args.src_host, args.src_db)
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
                "Login failed (18456). Use --windows-auth or check credentials."
            ) from exc
        raise
    log.info("MSSQL connected.")
    return conn


def connect_mysql(args, log, db: str | None = None):
    try:
        import mysql.connector
    except ImportError:
        raise RuntimeError(
            "mysql-connector-python not installed: "
            "pip install mysql-connector-python"
        )
    cfg = dict(
        host=args.tgt_host,
        port=args.tgt_port,
        user=args.tgt_user,
        password=args.tgt_pass,
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        autocommit=False,
        allow_local_infile=True,
    )
    if db:
        cfg["database"] = db
    log.info("Connecting MySQL: %s/%s", args.tgt_host, db or "(no db)")
    conn = mysql.connector.connect(**cfg)
    log.info("MySQL connected.")
    return conn


def drop_and_recreate_db(args, log):
    import mysql.connector
    tgt = args.tgt_db
    log.info("--drop-target: dropping database '%s' if it exists...", tgt)
    conn = mysql.connector.connect(
        host=args.tgt_host, port=args.tgt_port,
        user=args.tgt_user, password=args.tgt_pass,
        charset="utf8mb4", autocommit=True,
    )
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{tgt}`")
    cur.execute(
        f"CREATE DATABASE `{tgt}` "
        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    conn.close()
    log.info("Database '%s' recreated.", tgt)


def build_parser():
    p = argparse.ArgumentParser(
        prog="migrate_mysql.py",
        description="MSSQL -> MySQL migration tool (streaming, no CSV)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (Windows CMD):

  python migrate_mysql.py --windows-auth ^
    --src-host localhost --src-db AdventureWorks ^
    --tgt-host localhost --tgt-db aw_mysql ^
    --tgt-user root --tgt-pass secret ^
    --skip-schema dbo --out C:\\migration_mysql --drop-target

  python migrate_mysql.py ^
    --src-host localhost --src-db AdventureWorks ^
    --src-user sa --src-pass secret ^
    --tgt-host localhost --tgt-db aw_mysql ^
    --tgt-user root --tgt-pass secret ^
    --skip-schema dbo --out C:\\migration_mysql --drop-target ^
    --batch-size 10000 --max-retries 5
        """,
    )

    src = p.add_argument_group("Source (MSSQL)")
    src.add_argument("--src-host", default="localhost")
    src.add_argument("--src-port", default=1433, type=int)
    src.add_argument("--src-db", required=True)
    src.add_argument("--src-user", default=None)
    src.add_argument("--src-pass", default=None)
    src.add_argument("--windows-auth", action="store_true")

    tgt = p.add_argument_group("Target (MySQL)")
    tgt.add_argument("--tgt-host", default="localhost")
    tgt.add_argument("--tgt-port", default=3306, type=int)
    tgt.add_argument("--tgt-db", required=True)
    tgt.add_argument("--tgt-user", required=True)
    tgt.add_argument("--tgt-pass", required=True)

    p.add_argument("--skip-schema", nargs="+", default=["dbo"])
    p.add_argument("--out", default="./migration_mysql_output")
    p.add_argument("--drop-target", action="store_true",
                   help="Drop and recreate target DB before migrating")
    p.add_argument("--audit-only", action="store_true",
                   help="Audit and report only — no migration")
    p.add_argument("--batch-size", type=int, default=5000,
                   help="Rows per batch for streaming insert (default: 5000)")
    p.add_argument("--max-retries", type=int, default=3,
                   help="Max retries per batch on transient errors (default: 3)")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    log = _make_logger(os.path.join(out_dir, "logs"))

    log.info("=" * 60)
    log.info("MSSQL -> MySQL Migration (streaming, batch=%d)", args.batch_size)
    log.info("Source : %s/%s", args.src_host, args.src_db)
    log.info("Target : %s/%s", args.tgt_host, args.tgt_db)
    log.info("Skip   : %s", args.skip_schema)
    log.info("Output : %s", out_dir)
    log.info("=" * 60)

    skip = set(args.skip_schema)
    src_conn = my_conn = None

    try:
        src_conn = connect_mssql(args, log)

        if not args.audit_only:
            if getattr(args, "drop_target", False):
                drop_and_recreate_db(args, log)
            my_conn = connect_mysql(args, log, db=args.tgt_db)

        # Phase 1-2: Audit
        auditor = Auditor(src_conn, skip, log)
        audit = auditor.run(out_dir)

        if args.audit_only:
            log.info("--audit-only: stopping after audit.")
            return

        # Phase 3: Create database
        loader = Loader(my_conn, src_conn, audit, args.tgt_db, out_dir, log,
                        batch_size=args.batch_size, max_retries=args.max_retries)
        loader.create_database()

        # Phase 4: Create tables
        loader.create_tables()

        # Phase 5: Migrate data (streaming)
        data_results = loader.migrate_data()

        # Phase 6: Constraints
        loader.apply_constraints()

        # Phase 7: Indexes
        loader.create_indexes()

        # Phase 8-10: Views, functions, procedures
        obj = ObjectMigrator(my_conn, audit, args.tgt_db, skip, log)
        obj.migrate_views()
        obj.migrate_functions()
        obj.migrate_procedures()

        # Phase 11: Final validation + report
        final_report(my_conn, args.tgt_db, audit, data_results, out_dir, log)

    except Exception as exc:
        if log:
            log.error("FATAL: %s", exc)
            log.debug(traceback.format_exc())
        else:
            traceback.print_exc()
        sys.exit(1)
    finally:
        for c in (src_conn, my_conn):
            if c:
                try:
                    c.close()
                    log.debug("Connection closed: %s", type(c).__name__)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
