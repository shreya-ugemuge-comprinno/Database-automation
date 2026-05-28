"""
Transform Phase
───────────────
Reads the manifest.json produced by the extractor and:
1. Maps MSSQL data types -> PostgreSQL equivalents.
2. Lowercases all schema, table, column, index, and FK names.
3. Rewrites DDL to PostgreSQL syntax (CREATE TABLE, indexes, FKs).
4. Handles NULL coercions, encoding normalisation, reserved-word escaping.
5. Produces:
   • transformed/schema.sql        — full DDL ready to run on PostgreSQL
   • transformed/data/<table>.csv  — cleaned CSV files with lowercase headers
   • transformed/manifest.json     — updated manifest with lowercased names + pg_types
"""

import csv
import json
import os
import re
import sys
import unicodedata

from utils.logger import get_logger
from utils.type_mapping import map_type

logger = get_logger("transformer")

# Raise CSV field size limit to handle large binary/XML/text columns.
# Production.Document and similar tables exceed the 131072-byte default.
# Find the largest value the platform supports and apply it once at import time.
def _set_max_csv_field_size() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 2

_set_max_csv_field_size()

# ── PostgreSQL reserved words ─────────────────────────────────────────────────
# Identifiers matching these must be double-quoted even after lowercasing.
PG_RESERVED = {
    "all","analyse","analyze","and","any","array","as","asc","asymmetric",
    "authorization","binary","both","case","cast","check","collate","collation",
    "column","concurrently","constraint","create","cross","current_catalog",
    "current_date","current_role","current_schema","current_time",
    "current_timestamp","current_user","default","deferrable","desc","distinct",
    "do","else","end","except","false","fetch","for","foreign","freeze","from",
    "full","grant","group","having","ilike","in","initially","inner","intersect",
    "into","is","isnull","join","lateral","leading","left","like","limit",
    "localtime","localtimestamp","natural","not","notnull","null","offset","on",
    "only","or","order","outer","overlaps","placing","primary","references",
    "returning","right","select","session_user","similar","some","symmetric",
    "table","tablesample","then","to","trailing","true","union","unique","user",
    "using","variadic","verbose","when","where","window","with",
    # common MSSQL names that clash in PG
    "order","column","index","schema","type","value","values",
}


# ── Identifier helpers ────────────────────────────────────────────────────────

def to_lower(name: str) -> str:
    """
    Convert an identifier to lowercase.
    This is the single point of truth for the lowercasing requirement —
    applied to schemas, tables, columns, index names, FK names, and
    CSV header rows so every layer stays in sync.
    """
    return name.lower()


def quote_ident(name: str) -> str:
    """
    Double-quote a (already lowercased) identifier only when necessary:
    - It is a PostgreSQL reserved word, OR
    - It contains characters outside [a-z0-9_] or starts with a digit.
    Quoting is never wrong, but omitting it keeps the DDL readable.
    """
    low = name.lower()
    if low in PG_RESERVED or not re.match(r'^[a-z_][a-z0-9_]*$', low):
        return f'"{low}"'
    return low


# ── String / value helpers ────────────────────────────────────────────────────

def normalise_string(value: str) -> str:
    """NFC unicode normalisation + null-byte strip + whitespace trim."""
    if not value:
        return value
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\x00", "")
    return value.strip()


def coerce_null(value: str, pg_type: str) -> str:
    """
    Coerce empty strings to PostgreSQL's CSV NULL sentinel (\\N) for
    non-text types so COPY does not reject them.
    """
    text_types = ("text", "varchar", "char", "xml", "jsonb", "uuid")
    if value == "":
        if any(pg_type.startswith(t) for t in text_types):
            return value        # keep empty string for text columns
        return r"\N"            # NULL sentinel for numeric / date / bool etc.
    return value


# ── Main transformer ──────────────────────────────────────────────────────────

class DDLTransformer:
    def __init__(
        self,
        input_dir: str,
        output_dir: str,
        target: str = "postgres",
        source_schema: str = "dbo",
        target_schema: str = "public",
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.target = target
        self.source_schema = source_schema
        self.target_schema = target_schema

    def load_manifest(self) -> list[dict]:
        path = os.path.join(self.input_dir, "manifest.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ── Identifier normalisation ──────────────────────────────────────────────

    def _resolve_schema(self, raw_schema: str) -> str:
        """
        Return the target schema name for a given source schema.
        Non-dbo schemas are lowercased and kept as-is.
        """
        if raw_schema.lower() == self.source_schema.lower():
            return to_lower(self.target_schema)
        return to_lower(raw_schema)

    def _lowercase_table(self, table: dict) -> dict:
        """
        Return a new table dict with all identifiers lowercased:
        schema, name, column names, PK names, FK names/refs, index names/cols.
        The original source names are preserved under 'source_*' keys for
        the CSV reader (which needs to match the raw exported header).
        """
        t = dict(table)

        # Schema + table name
        t["source_schema"] = t["schema"]
        t["source_name"]   = t["name"]
        t["schema"] = self._resolve_schema(t["schema"])
        t["name"]   = to_lower(t["name"])

        # Columns — keep source_name for CSV header matching
        new_cols = []
        for col in t.get("columns", []):
            c = dict(col)
            c["source_name"] = c["name"]
            c["name"] = to_lower(c["name"])
            new_cols.append(c)
        t["columns"] = new_cols

        # Primary keys
        t["primary_keys"] = [to_lower(k) for k in t.get("primary_keys", [])]

        # Foreign keys
        new_fks = []
        for fk in t.get("foreign_keys", []):
            f = dict(fk)
            f["fk_name"]    = to_lower(f["fk_name"])
            f["column"]     = to_lower(f["column"])
            f["ref_table"]  = to_lower(f["ref_table"])
            f["ref_column"] = to_lower(f["ref_column"])
            new_fks.append(f)
        t["foreign_keys"] = new_fks

        # Indexes
        new_idx = []
        for idx in t.get("indexes", []):
            i = dict(idx)
            i["name"]    = to_lower(i["name"])
            i["columns"] = [to_lower(c) for c in i.get("columns", [])]
            new_idx.append(i)
        t["indexes"] = new_idx

        return t

    # ── DDL generation ────────────────────────────────────────────────────────

    def build_column_ddl(self, col: dict) -> tuple[str, str]:
        """
        Returns (column_ddl_line, pg_type) for one column.
        col["name"] is already lowercased at this point.
        """
        pg_type = map_type(
            col["data_type"],
            length=col.get("char_max_length"),
            precision=col.get("numeric_precision"),
            scale=col.get("numeric_scale"),
        )

        ident = quote_ident(col["name"])
        parts = [f"    {ident}  {pg_type}"]

        if col.get("is_identity"):
            pg_type_base = "bigserial" if "bigint" in pg_type else "serial"
            parts = [f"    {ident}  {pg_type_base}"]
        else:
            if not col.get("is_nullable"):
                parts.append("NOT NULL")
            default = col.get("column_default")
            if default:
                parts.append(f"DEFAULT {self._translate_default(default)}")

        return "  ".join(parts), pg_type

    def _translate_default(self, default: str) -> str:
        """Map common MSSQL default expressions to PostgreSQL equivalents."""
        d = default.strip().strip("()")
        mappings = {
            "getdate()":    "CURRENT_TIMESTAMP",
            "getutcdate()": "NOW()",
            "newid()":      "gen_random_uuid()",
            "0":  "0",
            "1":  "1",
            "''": "''",
            "n''": "''",
        }
        return mappings.get(d.lower(), d)

    def build_table_ddl(self, table: dict) -> str:
        """
        Generate a CREATE TABLE statement.
        All identifiers in `table` are already lowercased by _lowercase_table().
        """
        schema   = quote_ident(table["schema"])
        tbl_name = quote_ident(table["name"])
        lines: list[str] = []
        col_pg_types: dict[str, str] = {}

        for col in table["columns"]:
            col_line, pg_type = self.build_column_ddl(col)
            lines.append(col_line)
            col_pg_types[col["name"]] = pg_type   # keyed by lowercased name

        # Primary key
        if table.get("primary_keys"):
            pk_cols = ", ".join(quote_ident(c) for c in table["primary_keys"])
            lines.append(
                f"    CONSTRAINT pk_{table['name']}  PRIMARY KEY ({pk_cols})"
            )

        # Foreign keys
        for fk in table.get("foreign_keys", []):
            col_q     = quote_ident(fk["column"])
            ref_tbl   = quote_ident(fk["ref_table"])
            ref_col   = quote_ident(fk["ref_column"])
            fk_name   = quote_ident(fk["fk_name"])
            on_del    = fk.get("on_delete", "NO_ACTION").replace("_", " ")
            lines.append(
                f"    CONSTRAINT {fk_name}  FOREIGN KEY ({col_q})\n"
                f"        REFERENCES {ref_tbl} ({ref_col})\n"
                f"        ON DELETE {on_del}"
            )

        body = ",\n".join(lines)
        ddl  = f"CREATE TABLE IF NOT EXISTS {schema}.{tbl_name} (\n{body}\n);\n"

        table["_pg_types"] = col_pg_types
        return ddl

    def build_index_ddl(self, table: dict) -> list[str]:
        """Generate CREATE INDEX statements. All names already lowercased."""
        schema   = quote_ident(table["schema"])
        tbl_name = quote_ident(table["name"])
        stmts: list[str] = []
        for idx in table.get("indexes", []):
            unique   = "UNIQUE " if idx.get("unique") else ""
            idx_name = quote_ident(idx["name"])
            cols     = ", ".join(quote_ident(c) for c in idx.get("columns", []))
            stmts.append(
                f"CREATE {unique}INDEX CONCURRENTLY IF NOT EXISTS {idx_name}\n"
                f"    ON {schema}.{tbl_name} USING btree ({cols});\n"
            )
        return stmts

    # ── CSV cleaning ──────────────────────────────────────────────────────────

    def clean_csv(self, table: dict) -> str:
        """
        Read the raw CSV (original mixed-case headers from MSSQL),
        write a cleaned CSV with:
          - Lowercased header row (matching the lowercased DDL columns)
          - Unicode NFC normalisation on values
          - NULL coercion for non-text types
        Returns the output CSV path.
        """
        src = os.path.join(self.input_dir, "data", table["csv_file"])
        os.makedirs(os.path.join(self.output_dir, "data"), exist_ok=True)
        dst = os.path.join(self.output_dir, "data", table["csv_file"])

        pg_types = table.get("_pg_types", {})
        # lowercased col names in order (for pg_type lookup)
        lc_col_names = [c["name"] for c in table["columns"]]

        with open(src, newline="", encoding="utf-8") as fin, \
             open(dst, "w", newline="", encoding="utf-8") as fout:

            reader = csv.reader(fin)
            writer = csv.writer(fout, quoting=csv.QUOTE_MINIMAL)

            for i, row in enumerate(reader):
                if i == 0:
                    # Header row — lowercase every column name
                    writer.writerow([to_lower(h) for h in row])
                    continue

                cleaned = []
                for j, val in enumerate(row):
                    lc_col = lc_col_names[j] if j < len(lc_col_names) else ""
                    pg_type = pg_types.get(lc_col, "text")
                    val = normalise_string(val)
                    val = coerce_null(val, pg_type)
                    cleaned.append(val)
                writer.writerow(cleaned)

        logger.info("  Cleaned CSV (lowercase headers): %s", dst)
        return dst

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self):
        os.makedirs(self.output_dir, exist_ok=True)
        manifest = self.load_manifest()
        logger.info("Transforming %d tables.", len(manifest))

        all_ddl: list[str] = [
            "-- Generated by etl_migrator\n"
            "-- Target: PostgreSQL\n"
            "-- All identifiers lowercased for PG naming convention\n"
        ]
        out_manifest = []

        for raw_table in manifest:
            src_label = f"{raw_table['schema']}.{raw_table['name']}"
            logger.info("Transforming: %s", src_label)

            # ── Lowercase all identifiers ────────────────────────────────────
            table = self._lowercase_table(raw_table)
            tgt_label = f"{table['schema']}.{table['name']}"
            if src_label.lower() != tgt_label:
                logger.info("  Renamed: %s -> %s", src_label, tgt_label)

            # ── DDL ──────────────────────────────────────────────────────────
            table_ddl  = self.build_table_ddl(table)
            index_ddls = self.build_index_ddl(table)
            all_ddl.append(f"-- Source: {src_label}  ->  Target: {tgt_label}")
            all_ddl.append(table_ddl)
            all_ddl.extend(index_ddls)

            # ── CSV ──────────────────────────────────────────────────────────
            self.clean_csv(table)

            out_manifest.append({
                **{k: v for k, v in table.items() if not k.startswith("_")},
                "pg_types": table.get("_pg_types", {}),
            })

        # Write schema.sql
        schema_path = os.path.join(self.output_dir, "schema.sql")
        with open(schema_path, "w", encoding="utf-8") as f:
            f.write("\n".join(all_ddl))
        logger.info("Schema SQL written: %s", schema_path)

        # Write manifest
        manifest_path = os.path.join(self.output_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(out_manifest, f, indent=2)
        logger.info("Manifest written: %s", manifest_path)
