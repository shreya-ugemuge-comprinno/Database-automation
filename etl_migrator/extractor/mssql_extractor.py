"""
Extract Phase
─────────────
1. Connect to MSSQL using pyodbc.
2. Query information_schema + sys tables to get full DDL metadata
   (tables, columns, PKs, FKs, indexes, views, functions, procedures).
3. Export each table's data to a CSV file.
4. Write metadata to a JSON manifest consumed by the transformer.
"""

import csv
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

from utils.logger import get_logger

logger = get_logger("extractor")


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

# ── ODBC types pyodbc cannot fetch natively ───────────────────────────────────
# These are cast to NVARCHAR(MAX) in the SELECT so pyodbc can read them as text.
# type=-151 geography, type=-150 geometry, type=-150 hierarchyid, sql_variant etc.
CAST_TO_NVARCHAR = {
    "geography", "geometry", "hierarchyid", "sql_variant",
    "xml",          # xml can also cause issues; cast to string is safe
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ColumnMeta:
    name: str
    ordinal: int
    data_type: str
    char_max_length: str | None
    numeric_precision: str | None
    numeric_scale: str | None
    is_nullable: bool
    column_default: str | None
    is_identity: bool


@dataclass
class TableMeta:
    schema: str
    name: str
    columns: list[ColumnMeta] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)
    indexes: list[dict] = field(default_factory=list)
    row_count: int = 0


# ── Extractor ─────────────────────────────────────────────────────────────────

class MSSQLExtractor:
    def __init__(self, host: str, port: int, database: str,
                 user: str, password: str, schema: str, output_dir: str,
                 skip_schemas: list[str] | None = None):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.output_dir = output_dir
        self.conn = None
        # Schemas to skip entirely (case-insensitive). Defaults to skipping 'dbo'.
        self.skip_schemas: set[str] = {
            s.lower() for s in (skip_schemas if skip_schemas is not None else ["dbo"])
        }

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, windows_auth: bool = False):
        try:
            import pyodbc
        except ImportError:
            raise RuntimeError("pyodbc not installed. Run: pip install pyodbc")

        if windows_auth or not self.user:
            # Windows Authentication — uses the currently logged-in Windows user.
            # No UID/PWD needed. Works when SQL Server is on the same machine
            # or the Windows user has been granted SQL Server access.
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={self.host},{self.port};"
                f"DATABASE={self.database};"
                f"Trusted_Connection=yes;"
                f"TrustServerCertificate=yes;"
            )
            logger.info("Connecting to MSSQL (Windows Auth): %s/%s", self.host, self.database)
        else:
            # SQL Server Authentication — requires sa (or another SQL login)
            # with Mixed Mode auth enabled on the server.
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={self.host},{self.port};"
                f"DATABASE={self.database};"
                f"UID={self.user};"
                f"PWD={self.password};"
                f"TrustServerCertificate=yes;"
            )
            logger.info("Connecting to MSSQL (SQL Auth) as [%s]: %s/%s",
                        self.user, self.host, self.database)

        try:
            self.conn = pyodbc.connect(conn_str)
        except pyodbc.InterfaceError as exc:
            code = str(exc)
            if "28000" in code or "18456" in code:
                raise RuntimeError(
                    "Login failed (error 18456). Possible causes:\n"
                    "  1. Wrong username or password.\n"
                    "  2. SQL Server is set to Windows Authentication only.\n"
                    "     Fix: SSMS -> Server Properties -> Security -> enable\n"
                    "     'SQL Server and Windows Authentication mode', then restart.\n"
                    "  3. The 'sa' account is disabled.\n"
                    "     Fix: ALTER LOGIN sa ENABLE; ALTER LOGIN sa WITH PASSWORD='...';\n"
                    "  4. Try Windows Auth instead: add --windows-auth to your command."
                ) from exc
            raise
        logger.info("Connected.")

    def disconnect(self):
        if self.conn:
            self.conn.close()
            logger.info("Disconnected from MSSQL.")

    # ── Schema extraction ─────────────────────────────────────────────────────

    def get_tables(self) -> list[tuple[str, str]]:
        """
        Return (schema, table_name) pairs for all BASE TABLE objects,
        excluding any schema listed in self.skip_schemas.

        When self.schema is set to a specific schema (not '%'), restrict
        to that schema only — but still honour skip_schemas so 'dbo' is
        always excluded even if the user explicitly passed --schema dbo.
        """
        sql = """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM   INFORMATION_SCHEMA.TABLES
            WHERE  TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
        cur = self.conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()

        results = []
        for schema_name, table_name in rows:
            if schema_name.lower() in self.skip_schemas:
                logger.info("  Skipping [%s].[%s] — schema in skip list", schema_name, table_name)
                continue
            # If caller specified an explicit schema filter (not the wildcard default),
            # honour it — but dbo is still skipped even if explicitly requested.
            if self.schema and self.schema.lower() not in ("*", "%", "all"):
                if schema_name.lower() != self.schema.lower():
                    continue
            results.append((schema_name, table_name))
        return results

    def get_columns(self, table: str, schema_name: str | None = None) -> list[ColumnMeta]:
        sql = """
            SELECT
                c.COLUMN_NAME,
                c.ORDINAL_POSITION,
                c.DATA_TYPE,
                c.CHARACTER_MAXIMUM_LENGTH,
                c.NUMERIC_PRECISION,
                c.NUMERIC_SCALE,
                c.IS_NULLABLE,
                c.COLUMN_DEFAULT,
                COLUMNPROPERTY(
                    OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME),
                    c.COLUMN_NAME, 'IsIdentity'
                ) AS IS_IDENTITY
            FROM   INFORMATION_SCHEMA.COLUMNS c
            WHERE  c.TABLE_SCHEMA = ?
              AND  c.TABLE_NAME   = ?
            ORDER BY c.ORDINAL_POSITION
        """
        cur = self.conn.cursor()
        cur.execute(sql, schema_name or self.schema, table)
        cols = []
        for row in cur.fetchall():
            cols.append(ColumnMeta(
                name=row[0],
                ordinal=row[1],
                data_type=row[2],
                char_max_length=str(row[3]) if row[3] is not None else None,
                numeric_precision=str(row[4]) if row[4] is not None else None,
                numeric_scale=str(row[5]) if row[5] is not None else None,
                is_nullable=(row[6] == "YES"),
                column_default=row[7],
                is_identity=bool(row[8]),
            ))
        return cols

    def get_primary_keys(self, table: str, schema_name: str | None = None) -> list[str]:
        sql = """
            SELECT kcu.COLUMN_NAME
            FROM   INFORMATION_SCHEMA.TABLE_CONSTRAINTS   tc
            JOIN   INFORMATION_SCHEMA.KEY_COLUMN_USAGE    kcu
                   ON  tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                   AND tc.TABLE_SCHEMA    = kcu.TABLE_SCHEMA
            WHERE  tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
              AND  tc.TABLE_SCHEMA    = ?
              AND  tc.TABLE_NAME      = ?
            ORDER BY kcu.ORDINAL_POSITION
        """
        cur = self.conn.cursor()
        cur.execute(sql, schema_name or self.schema, table)
        return [row[0] for row in cur.fetchall()]

    def get_foreign_keys(self, table: str, schema_name: str | None = None) -> list[dict]:
        sql = """
            SELECT
                fk.name                         AS fk_name,
                COL_NAME(fc.parent_object_id,
                         fc.parent_column_id)   AS col,
                OBJECT_NAME(fk.referenced_object_id) AS ref_table,
                COL_NAME(fc.referenced_object_id,
                         fc.referenced_column_id) AS ref_col,
                fk.delete_referential_action_desc,
                fk.update_referential_action_desc
            FROM   sys.foreign_keys          fk
            JOIN   sys.foreign_key_columns   fc
                   ON fc.constraint_object_id = fk.object_id
            WHERE  OBJECT_NAME(fk.parent_object_id) = ?
              AND  SCHEMA_NAME(fk.schema_id)         = ?
        """
        cur = self.conn.cursor()
        cur.execute(sql, table, schema_name or self.schema)
        fks = []
        for row in cur.fetchall():
            fks.append({
                "fk_name":    row[0],
                "column":     row[1],
                "ref_table":  row[2],
                "ref_column": row[3],
                "on_delete":  row[4],
                "on_update":  row[5],
            })
        return fks

    def get_indexes(self, table: str) -> list[dict]:
        sql = """
            SELECT
                i.name                              AS index_name,
                i.is_unique,
                i.type_desc,
                STRING_AGG(c.name, ',')
                    WITHIN GROUP (ORDER BY ic.key_ordinal) AS columns
            FROM   sys.indexes            i
            JOIN   sys.index_columns      ic
                   ON ic.object_id = i.object_id
                  AND ic.index_id  = i.index_id
            JOIN   sys.columns            c
                   ON c.object_id  = ic.object_id
                  AND c.column_id  = ic.column_id
            WHERE  OBJECT_NAME(i.object_id) = ?
              AND  i.is_primary_key          = 0
              AND  i.name IS NOT NULL
            GROUP BY i.name, i.is_unique, i.type_desc
        """
        cur = self.conn.cursor()
        cur.execute(sql, table)
        indexes = []
        for row in cur.fetchall():
            indexes.append({
                "name":     row[0],
                "unique":   bool(row[1]),
                "type":     row[2],
                "columns":  row[3].split(",") if row[3] else [],
            })
        return indexes

    def get_row_count(self, table: str, schema_name: str | None = None) -> int:
        s = schema_name or self.schema
        cur = self.conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM [{s}].[{table}]")
        return cur.fetchone()[0]

    # ── Data export ───────────────────────────────────────────────────────────

    def _build_select_expr(self, col: ColumnMeta) -> str:
        """
        Return the SELECT expression for a single column.

        Columns whose MSSQL type pyodbc cannot fetch natively (geography,
        geometry, hierarchyid, xml, sql_variant) are cast to NVARCHAR(MAX)
        so they arrive as plain text strings rather than causing
        'ODBC SQL type -151 is not yet supported' errors.
        """
        col_q = f"[{col.name}]"
        if col.data_type.lower() in CAST_TO_NVARCHAR:
            logger.debug(
                "    Casting column [%s] (%s) to NVARCHAR(MAX)",
                col.name, col.data_type,
            )
            return f"CAST({col_q} AS NVARCHAR(MAX)) AS {col_q}"
        return col_q

    def export_table_csv(self, table: str, columns: list[ColumnMeta], schema_name: str | None = None) -> str:
        """
        Export all rows of a table to CSV.

        Columns with types that pyodbc cannot handle natively (geography,
        geometry, hierarchyid, xml, sql_variant) are cast to NVARCHAR(MAX)
        in the SELECT statement so the data arrives as text.

        Returns the output file path.
        """
        s = schema_name or self.schema
        os.makedirs(os.path.join(self.output_dir, "data"), exist_ok=True)
        # Prefix filename with schema name to avoid cross-schema collisions
        out_path = os.path.join(self.output_dir, "data", f"{s}__{table}.csv")
        col_names = [c.name for c in columns]

        select_exprs = ", ".join(self._build_select_expr(c) for c in columns)
        sql = f"SELECT {select_exprs} FROM [{s}].[{table}]"

        cur = self.conn.cursor()
        try:
            cur.execute(sql)
        except Exception as exc:
            logger.error("Failed to query [%s].[%s]: %s", s, table, exc)
            raise

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(col_names)   # header row (original casing; lowercased later)
            batch_size = 5000
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    writer.writerow([
                        "" if v is None else str(v)
                        for v in row
                    ])

        logger.info("  Exported %s.%s -> %s", s, table, out_path)
        return out_path

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self, windows_auth: bool = False):
        os.makedirs(self.output_dir, exist_ok=True)
        self.connect(windows_auth=windows_auth)

        try:
            table_pairs = self.get_tables()
            skipped = {s.lower() for s in self.skip_schemas}
            logger.info(
                "Found %d migratable tables (skip_schemas=%s)",
                len(table_pairs), sorted(skipped),
            )

            manifest: list[dict[str, Any]] = []

            for schema_name, table_name in table_pairs:
                logger.info("Processing table: %s.%s", schema_name, table_name)

                columns   = self.get_columns(table_name, schema_name)
                pks       = self.get_primary_keys(table_name, schema_name)
                fks       = self.get_foreign_keys(table_name, schema_name)
                indexes   = self.get_indexes(table_name)
                row_count = self.get_row_count(table_name, schema_name)
                csv_path  = self.export_table_csv(table_name, columns, schema_name)

                meta = TableMeta(
                    schema=schema_name,
                    name=table_name,
                    columns=columns,
                    primary_keys=pks,
                    foreign_keys=fks,
                    indexes=indexes,
                    row_count=row_count,
                )
                manifest.append({
                    **asdict(meta),
                    "csv_file": os.path.basename(csv_path),
                })

            # Write manifest
            manifest_path = os.path.join(self.output_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

            logger.info("Manifest written: %s", manifest_path)
            logger.info("Extract complete. %d tables processed.", len(table_pairs))

        finally:
            self.disconnect()
