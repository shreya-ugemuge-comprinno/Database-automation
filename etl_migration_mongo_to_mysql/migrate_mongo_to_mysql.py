#!/usr/bin/env python3
"""
migrate_mongo_to_mysql.py  —  MongoDB -> MySQL migration tool
==============================================================
7-phase pipeline:
  1.  Discover collections and sample documents
  2.  Infer schema (column types from full scan)
  3.  Create MySQL database + tables
  4.  Migrate data (batch INSERT)
  5.  Create indexes (from MongoDB indexes)
  6.  Validate row counts
  7.  Final report

Usage:
  python migrate_mongo_to_mysql.py ^
    --src-host localhost --src-db myapp ^
    --tgt-host localhost --tgt-db myapp_mysql ^
    --tgt-user root --tgt-pass secret ^
    --out ./migration_output --drop-target
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

from type_map import infer_mysql_type, widen_type, convert_for_mysql

# ── Logging ───────────────────────────────────────────────────────────────────

import io
import logging
from logging.handlers import RotatingFileHandler


def _make_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("migrate_mongo_to_mysql")
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
        os.path.join(log_dir, "migrate_mongo_to_mysql.log"),
        maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


# ── MySQL identifier quoting ───────────────────────────────────────────────────

def qi(name: str) -> str:
    """Backtick-quote a MySQL identifier."""
    return f"`{name}`"


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 1-2  —  Discovery & Schema Inference
# ═════════════════════════════════════════════════════════════════════════════

class Auditor:
    """Discover MongoDB collections and infer a relational schema."""

    def __init__(self, mongo_db, log, sample_size: int = 0):
        self.db = mongo_db
        self.log = log
        self.sample_size = sample_size  # 0 = full scan

    def run(self, out_dir: str) -> dict:
        self.log.info("=== PHASE 1-2: DISCOVERY & SCHEMA INFERENCE ===")
        collections = [
            c for c in self.db.list_collection_names()
            if not c.startswith("system.") and not c.startswith("_")
        ]
        collections.sort()
        self.log.info("Found %d collections", len(collections))

        manifest = []
        for coll_name in collections:
            self.log.info("  Scanning %s ...", coll_name)
            schema = self._infer_schema(coll_name)
            doc_count = self.db[coll_name].count_documents({})
            indexes = self._get_indexes(coll_name)
            manifest.append({
                "collection": coll_name,
                "target_table": coll_name.lower(),
                "columns": schema,
                "indexes": indexes,
                "source_doc_count": doc_count,
            })
            self.log.info("    %d docs, %d fields", doc_count, len(schema))

        audit = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tables": manifest,
        }

        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "audit.json"), "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)

        self.log.info("Discovery complete. Collections=%d", len(manifest))
        return audit

    def _infer_schema(self, coll_name: str) -> list[dict]:
        """Full-scan or sample to determine field names and MySQL types."""
        coll = self.db[coll_name]
        field_types: dict[str, str] = {}

        cursor = coll.find()
        if self.sample_size > 0:
            cursor = cursor.limit(self.sample_size)

        for doc in cursor:
            self._process_doc(doc, field_types)

        # Build ordered column list (_id first)
        cols = []
        if "_id" in field_types:
            cols.append({"field": "_id", "mysql_name": "_id",
                         "mysql_type": field_types.pop("_id")})
        for field in sorted(field_types.keys()):
            mysql_name = field.replace(".", "_").replace("$", "").lower()
            cols.append({"field": field, "mysql_name": mysql_name,
                         "mysql_type": field_types[field]})
        return cols

    def _process_doc(self, doc: dict, field_types: dict, prefix: str = ""):
        """Recursively flatten nested documents into dotted field names."""
        for key, value in doc.items():
            field = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(value, dict) and not any(
                k.startswith("$") for k in value.keys()
            ):
                # Nested sub-document — flatten
                self._process_doc(value, field_types, field + ".")
                continue

            inferred = infer_mysql_type(value)
            if field in field_types:
                field_types[field] = widen_type(field_types[field], inferred)
            else:
                field_types[field] = inferred

    def _get_indexes(self, coll_name: str) -> list[dict]:
        """Retrieve MongoDB indexes (excluding _id)."""
        coll = self.db[coll_name]
        indexes = []
        for idx in coll.list_indexes():
            if idx["name"] == "_id_":
                continue
            keys = list(idx["key"].keys())
            indexes.append({
                "name": idx["name"],
                "columns": [k.replace(".", "_").lower() for k in keys],
                "unique": idx.get("unique", False),
            })
        return indexes


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 3-5  —  Load (Create DB, Tables, Data, Indexes)
# ═════════════════════════════════════════════════════════════════════════════

class Loader:
    def __init__(self, my_conn, mongo_db, audit: dict,
                 tgt_db: str, log):
        self.my = my_conn
        self.mongo = mongo_db
        self.audit = audit
        self.tgt_db = tgt_db
        self.log = log

    def _my(self, sql: str, params=None):
        cur = self.my.cursor()
        cur.execute(sql, params or ())
        self.my.commit()
        return cur

    # ── Phase 3: create database + tables ─────────────────────────────────────

    def create_database(self):
        self.log.info("=== PHASE 3: CREATE DATABASE & TABLES ===")
        self._my(
            f"CREATE DATABASE IF NOT EXISTS {qi(self.tgt_db)} "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        self._my(f"USE {qi(self.tgt_db)}")
        self.log.info("  Database: %s", self.tgt_db)

    def create_tables(self):
        for t in self.audit["tables"]:
            sql = self._build_create_table(t)
            try:
                self._my(sql)
                self.log.info("  Created: %s", t["target_table"])
            except Exception as exc:
                self.log.error("  FAILED %s: %s", t["target_table"], exc)
                raise

    def _build_create_table(self, t: dict) -> str:
        table = t["target_table"]
        lines = []
        pk_col = None

        for col in t["columns"]:
            name = col["mysql_name"]
            mtype = col["mysql_type"]
            if col["field"] == "_id":
                lines.append(f"  {qi(name)}  {mtype} NOT NULL")
                pk_col = name
            else:
                lines.append(f"  {qi(name)}  {mtype} DEFAULT NULL")

        if pk_col:
            lines.append(f"  PRIMARY KEY ({qi(pk_col)})")

        body = ",\n".join(lines)
        return (
            f"CREATE TABLE IF NOT EXISTS {qi(self.tgt_db)}.{qi(table)} (\n"
            f"{body}\n"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;\n"
        )

    # ── Phase 4: migrate data ─────────────────────────────────────────────────

    def migrate_data(self) -> list[dict]:
        self.log.info("=== PHASE 4: DATA MIGRATION ===")
        self._my("SET FOREIGN_KEY_CHECKS = 0")
        results = []
        for t in self.audit["tables"]:
            results.append(self._load_collection(t))
        self._my("SET FOREIGN_KEY_CHECKS = 1")

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] != "PASS")
        self.log.info("  Data load: %d PASS  |  %d FAIL", passed, failed)
        return results

    def _load_collection(self, t: dict) -> dict:
        coll_name = t["collection"]
        table = t["target_table"]
        cols = t["columns"]

        try:
            col_names = [c["mysql_name"] for c in cols]
            field_names = [c["field"] for c in cols]
            col_list = ", ".join(qi(n) for n in col_names)
            placeholders = ", ".join(["%s"] * len(cols))
            sql = (f"INSERT INTO {qi(self.tgt_db)}.{qi(table)} "
                   f"({col_list}) VALUES ({placeholders})")

            coll = self.mongo[coll_name]
            cur = self.my.cursor()
            batch = []
            batch_size = 500

            for doc in coll.find():
                row = []
                for field in field_names:
                    val = self._get_nested(doc, field)
                    row.append(convert_for_mysql(val))
                batch.append(row)

                if len(batch) >= batch_size:
                    cur.executemany(sql, batch)
                    self.my.commit()
                    batch = []

            if batch:
                cur.executemany(sql, batch)
                self.my.commit()

            # Validate
            cur.execute(f"SELECT COUNT(*) FROM {qi(self.tgt_db)}.{qi(table)}")
            loaded = cur.fetchone()[0]
            src = t["source_doc_count"]
            ok = loaded == src
            status = "PASS" if ok else "FAIL"
            if ok:
                self.log.info("  [PASS] %s  %d rows", table, loaded)
            else:
                self.log.warning("  [FAIL] %s  src=%d tgt=%d", table, src, loaded)
            return {"table": table, "source_docs": src,
                    "target_rows": loaded, "status": status, "issues": []}

        except Exception as exc:
            try:
                self.my.rollback()
            except Exception:
                pass
            self.log.error("  [ERROR] %s: %s", table, exc)
            return {"table": table, "source_docs": t["source_doc_count"],
                    "target_rows": 0, "status": "ERROR", "issues": [str(exc)]}

    @staticmethod
    def _get_nested(doc: dict, field: str):
        """Retrieve a dotted-path field from a nested document."""
        parts = field.split(".")
        val = doc
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                return None
        return val

    # ── Phase 5: indexes ──────────────────────────────────────────────────────

    def create_indexes(self):
        self.log.info("=== PHASE 5: INDEXES ===")
        ok = skip = 0
        for t in self.audit["tables"]:
            table = t["target_table"]
            col_names = {c["mysql_name"] for c in t["columns"]}
            for idx in t["indexes"]:
                # Only create index if all columns exist in table
                if not all(c in col_names for c in idx["columns"]):
                    skip += 1
                    continue
                unique = "UNIQUE " if idx["unique"] else ""
                cols = ", ".join(qi(c) for c in idx["columns"])
                idx_name = idx["name"][:64]
                sql = (
                    f"CREATE {unique}INDEX {qi(idx_name)} "
                    f"ON {qi(self.tgt_db)}.{qi(table)} ({cols})"
                )
                try:
                    self._my(sql)
                    ok += 1
                except Exception as exc:
                    self.log.warning("  Skipped [%s]: %s", idx_name,
                                     str(exc)[:150])
                    skip += 1
        self.log.info("  Indexes: %d created, %d skipped", ok, skip)


# ═════════════════════════════════════════════════════════════════════════════
#  PHASE 6-7  —  Final Validation & Report
# ═════════════════════════════════════════════════════════════════════════════

def final_report(my_conn, tgt_db: str, audit: dict,
                 data_results: list[dict], out_dir: str, log) -> dict:
    log.info("=== PHASE 6-7: FINAL VALIDATION ===")
    cur = my_conn.cursor()
    table_reports = []

    for t in audit["tables"]:
        table = t["target_table"]
        try:
            cur.execute(f"SELECT COUNT(*) FROM {qi(tgt_db)}.{qi(table)}")
            tgt_rows = cur.fetchone()[0]
        except Exception:
            tgt_rows = -1

        src_docs = t["source_doc_count"]
        match = tgt_rows == src_docs
        table_reports.append({
            "table": table,
            "source_docs": src_docs,
            "target_rows": tgt_rows,
            "match": match,
            "status": "PASS" if match else "FAIL",
        })
        if match:
            log.info("  [PASS] %s  %d rows", table, tgt_rows)
        else:
            log.warning("  [FAIL] %s  src=%d tgt=%d", table, src_docs, tgt_rows)

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

def connect_mongodb(args, log):
    try:
        from pymongo import MongoClient
    except ImportError:
        raise RuntimeError("pymongo not installed: pip install pymongo")

    if args.src_user and args.src_pass:
        uri = (f"mongodb://{args.src_user}:{args.src_pass}@"
               f"{args.src_host}:{args.src_port}/{args.src_db}"
               f"?authSource={args.src_auth_db}")
    else:
        uri = f"mongodb://{args.src_host}:{args.src_port}"

    log.info("Connecting MongoDB: %s:%d/%s", args.src_host, args.src_port,
             args.src_db)
    client = MongoClient(uri)
    client.admin.command("ping")
    log.info("MongoDB connected.")
    return client


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
    )
    if db:
        cfg["database"] = db
    log.info("Connecting MySQL: %s/%s", args.tgt_host, db or "(no db)")
    conn = mysql.connector.connect(**cfg)
    log.info("MySQL connected.")
    return conn


def build_parser():
    p = argparse.ArgumentParser(
        prog="migrate_mongo_to_mysql.py",
        description="MongoDB -> MySQL migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  No auth (local MongoDB):
    python migrate_mongo_to_mysql.py ^
      --src-host localhost --src-db myapp ^
      --tgt-host localhost --tgt-db myapp_mysql ^
      --tgt-user root --tgt-pass secret ^
      --out ./migration_output --drop-target

  With MongoDB auth:
    python migrate_mongo_to_mysql.py ^
      --src-host localhost --src-db myapp ^
      --src-user mongoUser --src-pass mongoPass ^
      --tgt-host localhost --tgt-db myapp_mysql ^
      --tgt-user root --tgt-pass secret ^
      --out ./migration_output --drop-target
        """,
    )

    src = p.add_argument_group("Source (MongoDB)")
    src.add_argument("--src-host", default="localhost")
    src.add_argument("--src-port", default=27017, type=int)
    src.add_argument("--src-db", required=True)
    src.add_argument("--src-user", default=None)
    src.add_argument("--src-pass", default=None)
    src.add_argument("--src-auth-db", default="admin",
                     help="MongoDB authentication database")

    tgt = p.add_argument_group("Target (MySQL)")
    tgt.add_argument("--tgt-host", default="localhost")
    tgt.add_argument("--tgt-port", default=3306, type=int)
    tgt.add_argument("--tgt-db", required=True)
    tgt.add_argument("--tgt-user", required=True)
    tgt.add_argument("--tgt-pass", required=True)

    p.add_argument("--skip-collections", nargs="*", default=[],
                   help="Collections to skip (prefix _ always skipped)")
    p.add_argument("--sample-size", type=int, default=0,
                   help="Docs to sample for schema inference (0=full scan)")
    p.add_argument("--out", default="./migration_mongo_to_mysql_output")
    p.add_argument("--drop-target", action="store_true",
                   help="Drop and recreate target DB before migrating")
    p.add_argument("--audit-only", action="store_true",
                   help="Discover schema only — no migration")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    log = _make_logger(os.path.join(out_dir, "logs"))

    log.info("=" * 60)
    log.info("MongoDB -> MySQL Migration")
    log.info("Source : %s:%d/%s", args.src_host, args.src_port, args.src_db)
    log.info("Target : %s:%d/%s", args.tgt_host, args.tgt_port, args.tgt_db)
    log.info("Output : %s", out_dir)
    log.info("=" * 60)

    mongo_client = None
    my_conn = None

    try:
        mongo_client = connect_mongodb(args, log)
        mongo_db = mongo_client[args.src_db]

        # Phase 1-2: Discover & infer schema
        auditor = Auditor(mongo_db, log, sample_size=args.sample_size)
        audit = auditor.run(out_dir)

        if args.audit_only:
            log.info("--audit-only: stopping after discovery.")
            return

        # Connect MySQL
        if args.drop_target:
            import mysql.connector
            log.info("--drop-target: dropping '%s'...", args.tgt_db)
            tmp = mysql.connector.connect(
                host=args.tgt_host, port=args.tgt_port,
                user=args.tgt_user, password=args.tgt_pass,
                charset="utf8mb4", autocommit=True,
            )
            c = tmp.cursor()
            c.execute(f"DROP DATABASE IF EXISTS `{args.tgt_db}`")
            tmp.close()
            log.info("Database dropped.")

        my_conn = connect_mysql(args, log)

        # Phase 3: Create database + tables
        loader = Loader(my_conn, mongo_db, audit, args.tgt_db, log)
        loader.create_database()
        loader.create_tables()

        # Phase 4: Migrate data
        data_results = loader.migrate_data()

        # Phase 5: Indexes
        loader.create_indexes()

        # Phase 6-7: Final validation + report
        final_report(my_conn, args.tgt_db, audit, data_results, out_dir, log)

    except Exception as exc:
        if log:
            log.error("FATAL: %s", exc)
            log.debug(traceback.format_exc())
        else:
            traceback.print_exc()
        sys.exit(1)
    finally:
        if mongo_client:
            try:
                mongo_client.close()
            except Exception:
                pass
        if my_conn:
            try:
                my_conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
