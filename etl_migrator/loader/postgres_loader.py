"""
Load Phase
──────────
1. Connect to PostgreSQL.
2. Execute schema.sql to create all tables + indexes.
3. Bulk-load each CSV using PostgreSQL COPY for performance.
4. Run post-load validation: row count comparison + NULL checks.
5. Report any mismatches to the log and a validation_report.json.
"""

import csv
import json
import os
import io
import sys

from utils.logger import get_logger

logger = get_logger("loader")


# Allow large field values (binary, XML, long text) in CSV files.
def _set_max_csv_field_size() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 2

_set_max_csv_field_size()


class PostgresLoader:
    def __init__(self, host: str, port: int, database: str,
                 user: str, password: str, input_dir: str):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.input_dir = input_dir
        self.conn = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError("psycopg2 not installed. Run: pip install psycopg2-binary")

        logger.info("Connecting to PostgreSQL: %s/%s", self.host, self.database)
        self.conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
        )
        self.conn.autocommit = False
        logger.info("Connected.")

    def disconnect(self):
        if self.conn:
            self.conn.close()
            logger.info("Disconnected from PostgreSQL.")

    # ── Schema creation ───────────────────────────────────────────────────────

    def create_pg_schema(self, manifest: list[dict]):
        """
        Create all PostgreSQL schemas referenced in the manifest.
        In MSSQL 'dbo' is the default schema — PostgreSQL doesn't have it,
        so we must CREATE SCHEMA IF NOT EXISTS before any DDL runs.
        """
        schemas = {table["schema"] for table in manifest}
        cur = self.conn.cursor()
        for schema in schemas:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            logger.info("Ensured schema exists: %s", schema)
        self.conn.commit()

    def create_schema(self):
        """
        Execute the generated schema.sql file.

        Splits statements into two passes:
          1. All non-index DDL (CREATE TABLE, constraints) — run inside a
             single transaction so it rolls back cleanly on failure.
          2. CREATE INDEX CONCURRENTLY statements — must run outside any
             transaction block (autocommit=True), one at a time.
        """
        sql_path = os.path.join(self.input_dir, "schema.sql")
        with open(sql_path, encoding="utf-8") as f:
            sql = f.read()

        logger.info("Executing schema DDL...")

        # Split on semicolons, strip blanks and pure-comment lines
        raw_stmts = [
            s.strip() for s in sql.split(";")
            if s.strip() and not s.strip().startswith("--")
        ]

        table_stmts = []
        index_stmts = []
        for stmt in raw_stmts:
            first_line = stmt.lstrip().split("\n")[0].upper()
            if "CREATE" in first_line and "INDEX" in first_line and "CONCURRENTLY" in first_line:
                index_stmts.append(stmt)
            else:
                table_stmts.append(stmt)

        # ── Pass 1: table DDL inside a transaction ────────────────────────────
        cur = self.conn.cursor()
        self.conn.autocommit = False
        for stmt in table_stmts:
            try:
                cur.execute(stmt)
            except Exception as exc:
                self.conn.rollback()
                logger.error("DDL failed:\n%s\nError: %s", stmt, exc)
                raise
        self.conn.commit()
        logger.info("Table DDL applied (%d statements).", len(table_stmts))

        # ── Pass 2: indexes outside transaction (autocommit) ──────────────────
        if index_stmts:
            self.conn.autocommit = True
            cur = self.conn.cursor()
            for stmt in index_stmts:
                try:
                    cur.execute(stmt)
                    idx_name = stmt.split("INDEX")[1].split("\n")[0].strip() if "INDEX" in stmt else "?"
                    logger.info("  Index created: %s", idx_name.split("ON")[0].strip())
                except Exception as exc:
                    # Non-fatal: log and continue — indexes can be rebuilt later
                    logger.warning("  Index creation failed (skipped): %s", exc)
            self.conn.autocommit = False
            logger.info("Index DDL applied (%d indexes).", len(index_stmts))

        logger.info("Schema created successfully.")

    # ── Data loading ──────────────────────────────────────────────────────────

    def load_table(self, table: dict) -> int:
        """
        Bulk-load one table using COPY FROM STDIN.
        Returns number of rows loaded.
        """
        schema   = table["schema"]
        tbl_name = table["name"]
        csv_path = os.path.join(self.input_dir, "data", table["csv_file"])
        col_names = [c["name"] for c in table["columns"]]
        cols_sql  = ", ".join(f'"{c}"' for c in col_names)

        copy_sql = (
            f'COPY "{schema}"."{tbl_name}" ({cols_sql}) '
            f"FROM STDIN WITH (FORMAT csv, HEADER true, NULL '\\N', ENCODING 'UTF8')"
        )

        cur = self.conn.cursor()
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                cur.copy_expert(copy_sql, f)
            self.conn.commit()
            # Get loaded count
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{tbl_name}"')
            loaded = cur.fetchone()[0]
            logger.info("  Loaded %s.%s -> %d rows", schema, tbl_name, loaded)
            return loaded
        except Exception as exc:
            self.conn.rollback()
            logger.error("  COPY failed for %s.%s: %s", schema, tbl_name, exc)
            raise

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_table(self, table: dict, loaded_rows: int) -> dict:
        """
        Post-load validation checks:
        1. Row count matches source.
        2. NOT NULL columns have no NULLs.
        Returns a validation result dict.
        """
        schema   = table["schema"]
        tbl_name = table["name"]
        src_rows = table.get("row_count", 0)
        cur = self.conn.cursor()

        issues = []

        # Check 1: row count
        cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{tbl_name}"')
        actual_rows = cur.fetchone()[0]
        if actual_rows != src_rows:
            msg = f"Row count mismatch: source={src_rows}, target={actual_rows}"
            issues.append(msg)
            logger.warning("  [MISMATCH] %s.%s — %s", schema, tbl_name, msg)
        else:
            logger.info("  [OK] %s.%s row count: %d", schema, tbl_name, actual_rows)

        # Check 2: NOT NULL columns contain no NULLs
        not_null_cols = [
            c["name"] for c in table["columns"]
            if not c.get("is_nullable") and not c.get("is_identity")
        ]
        for col in not_null_cols:
            cur.execute(
                f'SELECT COUNT(*) FROM "{schema}"."{tbl_name}" WHERE "{col}" IS NULL'
            )
            null_count = cur.fetchone()[0]
            if null_count > 0:
                msg = f'Column "{col}" has {null_count} unexpected NULLs'
                issues.append(msg)
                logger.warning("  [MISMATCH] %s.%s — %s", schema, tbl_name, msg)

        return {
            "table":        f"{schema}.{tbl_name}",
            "source_rows":  src_rows,
            "target_rows":  actual_rows,
            "issues":       issues,
            "status":       "PASS" if not issues else "FAIL",
        }

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self):
        manifest_path = os.path.join(self.input_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        self.connect()
        try:
            # Step 1: Ensure target schemas exist, then run DDL
            self.create_pg_schema(manifest)
            self.create_schema()

            # Step 2: Load data
            validation_results = []
            for table in manifest:
                logger.info("Loading: %s.%s", table["schema"], table["name"])
                try:
                    loaded = self.load_table(table)
                    result = self.validate_table(table, loaded)
                except Exception as exc:
                    result = {
                        "table":  f"{table['schema']}.{table['name']}",
                        "issues": [str(exc)],
                        "status": "ERROR",
                    }
                validation_results.append(result)

            # Step 3: Validation report
            report_path = os.path.join(self.input_dir, "validation_report.json")
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(validation_results, f, indent=2)

            # Summary
            passed = sum(1 for r in validation_results if r["status"] == "PASS")
            failed = sum(1 for r in validation_results if r["status"] != "PASS")
            logger.info("=" * 50)
            logger.info("Validation summary: %d PASS  |  %d FAIL/ERROR", passed, failed)
            logger.info("Full report: %s", report_path)

            if failed:
                logger.warning("Some tables failed validation. Check validation_report.json.")

        finally:
            self.disconnect()
