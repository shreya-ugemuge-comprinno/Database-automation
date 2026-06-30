#!/usr/bin/env python3
"""
migrate_mongodb.py  —  MSSQL -> MongoDB migration tool
=======================================================
8-phase pipeline:
  1.  Audit source database
  2.  Generate datatype mapping + compatibility report
  3.  Create MongoDB database/collections
  4.  Migrate data + validate document counts
  5.  Create indexes (including unique constraints)
  6.  Migrate views (as materialized collections via aggregation pipelines)
  7.  Log functions/procedures (manual rewrite needed)
  8.  Final validation + migration report

Usage:
  Windows Authentication:
    python migrate_mongodb.py --windows-auth ^
      --src-host localhost --src-db AdventureWorks ^
      --tgt-host localhost --tgt-db aw_mongo ^
      --skip-schema dbo --out C:\\migration_mongodb --drop-target

  SQL Server Authentication:
    python migrate_mongodb.py ^
      --src-host localhost --src-db AdventureWorks ^
      --src-user sa --src-pass secret ^
      --tgt-host localhost --tgt-db aw_mongo ^
      --skip-schema dbo --out C:\\migration_mongodb --drop-target
"""

import argparse
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from type_map import CAST_TYPES, get_bson_type, convert_value

# ── Logging ───────────────────────────────────────────────────────────────────

import io
import logging
from logging.handlers import RotatingFileHandler


def _make_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("migrate_mongodb")
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
        os.path.join(log_dir, "migrate_mongodb.log"),
        maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


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
            idx = self._get_indexes(cur, schema, table)
            rows = self._get_rowcount(cur, schema, table)
            manifest.append({
                "source_schema": schema,
                "source_table": table,
                "target_collection": f"{schema.lower()}_{table.lower()}",
                "columns": cols,
                "primary_keys": pks,
                "foreign_keys": fks,
                "unique_constraints": uqs,
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
            bson_type = get_bson_type(row[2])
            cols.append({
                "name": row[0],
                "field_name": row[0].lower(),
                "ordinal": row[1],
                "mssql_type": row[2],
                "bson_type": bson_type,
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
                COL_NAME(fc.referenced_object_id, fc.referenced_column_id)
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
            fks.append({
                "name": r[0].lower(),
                "column": r[1].lower(),
                "ref_collection": f"{ref_schema}_{r[3].lower()}",
                "ref_column": r[4].lower(),
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
        (r'\bEXEC\s*\(',       "BLOCKER", "Dynamic SQL via EXEC()"),
        (r'#\w+',              "BLOCKER", "Temp table (#table)"),
        (r'\bOPENROWSET\b',    "BLOCKER", "OPENROWSET not supported"),
        (r'\bFOR\s+XML\b',     "WARNING", "FOR XML — store as string field"),
        (r'\bCROSS\s+APPLY\b', "WARNING", "CROSS APPLY — use $lookup"),
        (r'\bOUTER\s+APPLY\b', "WARNING", "OUTER APPLY — use $lookup"),
        (r'\bPIVOT\b',         "WARNING", "PIVOT — use $group aggregation"),
        (r'\bTOP\s+\d+\b',     "INFO",    "TOP n — use $limit"),
        (r'\bISNULL\s*\(',     "INFO",    "ISNULL() — use $ifNull"),
        (r'@@ROWCOUNT',        "INFO",    "@@ROWCOUNT — use acknowledged writes"),
        (r'\bDATEADD\s*\(',    "INFO",    "DATEADD — use $dateAdd"),
        (r'\bGETDATE\s*\(\)',  "INFO",    "GETDATE() — use new Date()"),
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
#  PHASE 3-5  —  Load
# ═════════════════════════════════════════════════════════════════════════════

class Loader:
    def __init__(self, mongo_db, src_conn, audit: dict, work_dir: str, log):
        self.db = mongo_db
        self.src = src_conn
        self.audit = audit
        self.work = work_dir
        self.log = log

    # ── Phase 3: create collections ──────────────────────────────────────────

    def create_collections(self):
        self.log.info("=== PHASE 3: CREATE COLLECTIONS ===")
        for t in self.audit["tables"]:
            coll_name = t["target_collection"]
            if coll_name not in self.db.list_collection_names():
                self.db.create_collection(coll_name)
            self.log.info("  Collection: %s", coll_name)

    # ── Phase 4: migrate data ────────────────────────────────────────────────

    def migrate_data(self) -> list[dict]:
        self.log.info("=== PHASE 4: DATA MIGRATION ===")
        results = []
        for t in self.audit["tables"]:
            results.append(self._load_table(t))

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] != "PASS")
        self.log.info("  Data load: %d PASS  |  %d FAIL", passed, failed)
        return results

    def _load_table(self, t: dict) -> dict:
        schema = t["source_schema"]
        table = t["source_table"]
        coll_name = t["target_collection"]
        label = coll_name

        try:
            cols = t["columns"]
            col_exprs = []
            col_names = []
            for col in cols:
                name = col["name"]
                col_names.append(name)
                if col["needs_cast"]:
                    col_exprs.append(f"CAST([{name}] AS NVARCHAR(MAX)) AS [{name}]")
                else:
                    col_exprs.append(f"[{name}]")

            sql = f"SELECT {', '.join(col_exprs)} FROM [{schema}].[{table}]"
            cur = self.src.cursor()
            cur.execute(sql)

            coll = self.db[coll_name]
            batch = []
            batch_size = 1000
            total_inserted = 0

            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    doc = {}
                    for i, val in enumerate(row):
                        field = cols[i]["field_name"]
                        mssql_type = cols[i]["mssql_type"]
                        doc[field] = convert_value(val, mssql_type)
                    batch.append(doc)

                if len(batch) >= batch_size:
                    coll.insert_many(batch, ordered=False)
                    total_inserted += len(batch)
                    batch = []

            if batch:
                coll.insert_many(batch, ordered=False)
                total_inserted += len(batch)

            # Validate
            loaded = coll.count_documents({})
            src_rows = t["source_row_count"]
            ok = loaded == src_rows
            status = "PASS" if ok else "FAIL"
            if ok:
                self.log.info("  [PASS] %s  %d docs", label, loaded)
            else:
                self.log.warning("  [FAIL] %s  src=%d tgt=%d", label, src_rows, loaded)
            return {"table": label, "source_rows": src_rows,
                    "target_docs": loaded, "status": status, "issues": []}

        except Exception as exc:
            self.log.error("  [ERROR] %s: %s", label, exc)
            return {"table": label, "source_rows": t["source_row_count"],
                    "target_docs": 0, "status": "ERROR", "issues": [str(exc)]}

    # ── Phase 5: indexes ─────────────────────────────────────────────────────

    def create_indexes(self):
        self.log.info("=== PHASE 5: INDEXES ===")
        import pymongo
        ok = skip = 0

        for t in self.audit["tables"]:
            coll_name = t["target_collection"]
            coll = self.db[coll_name]

            # Unique indexes from unique constraints
            for uq in t["unique_constraints"]:
                keys = [(c, pymongo.ASCENDING) for c in uq["columns"]]
                try:
                    coll.create_index(keys, unique=True, name=uq["name"])
                    ok += 1
                except Exception as exc:
                    self.log.warning("  Skipped UQ [%s]: %s", uq["name"],
                                     str(exc)[:150])
                    skip += 1

            # Regular indexes
            for idx in t["indexes"]:
                keys = [(c, pymongo.ASCENDING) for c in idx["columns"]]
                try:
                    coll.create_index(keys, unique=idx["unique"],
                                      name=idx["name"])
                    ok += 1
                except Exception as exc:
                    self.log.warning("  Skipped IDX [%s]: %s", idx["name"],
                                     str(exc)[:150])
                    skip += 1

        self.log.info("  Indexes: %d created, %d skipped", ok, skip)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 6-7  —  Views, Functions, Procedures (logged only)
# ═════════════════════════════════════════════════════════════════════════════

class ObjectMigrator:
    def __init__(self, mongo_db, audit: dict, log):
        self.db = mongo_db
        self.audit = audit
        self.log = log

    def migrate_views(self):
        self.log.info("=== PHASE 6: VIEWS ===")
        views = self.audit.get("views", [])
        if not views:
            self.log.info("  No views found.")
            return

        # Store view definitions in a metadata collection for reference
        meta_coll = self.db["_migration_views"]
        docs = []
        for v in views:
            docs.append({
                "schema": v["schema"],
                "name": v["name"],
                "definition": v["definition"],
                "note": "Requires manual conversion to MongoDB aggregation pipeline",
            })
        if docs:
            meta_coll.insert_many(docs)
        self.log.info("  Views: %d logged to _migration_views collection "
                      "(manual rewrite to aggregation pipelines needed)", len(views))

    def migrate_functions(self):
        self.log.info("=== PHASE 7a: FUNCTIONS ===")
        funcs = self.audit.get("functions", [])
        for fn in funcs:
            self.log.warning("  Function %s.%s requires manual rewrite "
                             "(T-SQL -> application logic or aggregation)",
                             fn["schema"], fn["name"])
        self.log.info("  Functions: %d need manual rewrite", len(funcs))

    def migrate_procedures(self):
        self.log.info("=== PHASE 7b: STORED PROCEDURES ===")
        procs = self.audit.get("procedures", [])
        for p in procs:
            self.log.warning("  Procedure %s.%s requires manual rewrite "
                             "(T-SQL -> application logic or aggregation)",
                             p["schema"], p["name"])
        self.log.info("  Procedures: %d need manual rewrite", len(procs))


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 8  —  Final validation
# ═════════════════════════════════════════════════════════════════════════════

def final_report(mongo_db, audit: dict, data_results: list[dict],
                 out_dir: str, log) -> dict:
    log.info("=== PHASE 8: FINAL VALIDATION ===")
    table_reports = []

    for t in audit["tables"]:
        coll_name = t["target_collection"]
        try:
            doc_count = mongo_db[coll_name].count_documents({})
        except Exception:
            doc_count = -1

        src_rows = t["source_row_count"]
        match = doc_count == src_rows
        table_reports.append({
            "collection": coll_name,
            "source_rows": src_rows,
            "target_docs": doc_count,
            "match": match,
            "status": "PASS" if match else "FAIL",
        })
        if match:
            log.info("  [PASS] %s  %d docs", coll_name, doc_count)
        else:
            log.warning("  [FAIL] %s  src=%d tgt=%d", coll_name, src_rows, doc_count)

    passed = sum(1 for r in table_reports if r["status"] == "PASS")
    failed = sum(1 for r in table_reports if r["status"] != "PASS")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_collections": len(table_reports),
            "passed": passed,
            "failed": failed,
            "overall": "PASS" if failed == 0 else "FAIL",
        },
        "collections": table_reports,
        "views_logged": len(audit.get("views", [])),
        "functions_logged": len(audit.get("functions", [])),
        "procedures_logged": len(audit.get("procedures", [])),
    }

    path = os.path.join(out_dir, "migration_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    log.info("=" * 60)
    log.info("MIGRATION %s: %d/%d collections PASS",
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


def connect_mongodb(args, log):
    try:
        from pymongo import MongoClient
    except ImportError:
        raise RuntimeError("pymongo not installed: pip install pymongo")

    if args.tgt_user and args.tgt_pass:
        uri = (f"mongodb://{args.tgt_user}:{args.tgt_pass}@"
               f"{args.tgt_host}:{args.tgt_port}/{args.tgt_db}"
               f"?authSource={args.tgt_auth_db}")
    else:
        uri = f"mongodb://{args.tgt_host}:{args.tgt_port}"

    log.info("Connecting MongoDB: %s:%d/%s", args.tgt_host, args.tgt_port,
             args.tgt_db)
    client = MongoClient(uri)
    # Test connection
    client.admin.command("ping")
    log.info("MongoDB connected.")
    return client


def build_parser():
    p = argparse.ArgumentParser(
        prog="migrate_mongodb.py",
        description="MSSQL -> MongoDB migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Windows Authentication:
    python migrate_mongodb.py --windows-auth ^
      --src-host localhost --src-db AdventureWorks ^
      --tgt-host localhost --tgt-db aw_mongo ^
      --skip-schema dbo --out C:\\migration_mongodb --drop-target

  SQL Server Authentication:
    python migrate_mongodb.py ^
      --src-host localhost --src-db AdventureWorks ^
      --src-user sa --src-pass secret ^
      --tgt-host localhost --tgt-db aw_mongo ^
      --skip-schema dbo --out C:\\migration_mongodb --drop-target
        """,
    )

    src = p.add_argument_group("Source (MSSQL)")
    src.add_argument("--src-host", default="localhost")
    src.add_argument("--src-port", default=1433, type=int)
    src.add_argument("--src-db", required=True)
    src.add_argument("--src-user", default=None)
    src.add_argument("--src-pass", default=None)
    src.add_argument("--windows-auth", action="store_true")

    tgt = p.add_argument_group("Target (MongoDB)")
    tgt.add_argument("--tgt-host", default="localhost")
    tgt.add_argument("--tgt-port", default=27017, type=int)
    tgt.add_argument("--tgt-db", required=True)
    tgt.add_argument("--tgt-user", default=None)
    tgt.add_argument("--tgt-pass", default=None)
    tgt.add_argument("--tgt-auth-db", default="admin",
                     help="MongoDB authentication database")

    p.add_argument("--skip-schema", nargs="+", default=["dbo"])
    p.add_argument("--out", default="./migration_mongodb_output")
    p.add_argument("--drop-target", action="store_true",
                   help="Drop target database before migrating")
    p.add_argument("--audit-only", action="store_true",
                   help="Audit and report only — no migration")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    log = _make_logger(os.path.join(out_dir, "logs"))

    log.info("=" * 60)
    log.info("MSSQL -> MongoDB Migration")
    log.info("Source : %s/%s", args.src_host, args.src_db)
    log.info("Target : %s:%d/%s", args.tgt_host, args.tgt_port, args.tgt_db)
    log.info("Skip   : %s", args.skip_schema)
    log.info("Output : %s", out_dir)
    log.info("=" * 60)

    skip = set(args.skip_schema)
    src_conn = None
    mongo_client = None

    try:
        src_conn = connect_mssql(args, log)

        if not args.audit_only:
            mongo_client = connect_mongodb(args, log)
            if getattr(args, "drop_target", False):
                log.info("--drop-target: dropping database '%s'...", args.tgt_db)
                mongo_client.drop_database(args.tgt_db)
                log.info("Database '%s' dropped.", args.tgt_db)
            mongo_db = mongo_client[args.tgt_db]

        # Phase 1-2: Audit
        auditor = Auditor(src_conn, skip, log)
        audit = auditor.run(out_dir)

        if args.audit_only:
            log.info("--audit-only: stopping after audit.")
            return

        # Phase 3: Create collections
        loader = Loader(mongo_db, src_conn, audit, out_dir, log)
        loader.create_collections()

        # Phase 4: Migrate data
        data_results = loader.migrate_data()

        # Phase 5: Indexes
        loader.create_indexes()

        # Phase 6-7: Views, functions, procedures
        obj = ObjectMigrator(mongo_db, audit, log)
        obj.migrate_views()
        obj.migrate_functions()
        obj.migrate_procedures()

        # Phase 8: Final validation + report
        final_report(mongo_db, audit, data_results, out_dir, log)

    except Exception as exc:
        if log:
            log.error("FATAL: %s", exc)
            log.debug(traceback.format_exc())
        else:
            traceback.print_exc()
        sys.exit(1)
    finally:
        if src_conn:
            try:
                src_conn.close()
            except Exception:
                pass
        if mongo_client:
            try:
                mongo_client.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
