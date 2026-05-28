"""
MSSQL -> PostgreSQL data type mapping table.

Each entry:
    mssql_type (lowercase, no length/precision) -> pg_type string

For types that carry precision/scale (e.g. decimal, numeric, varchar)
the transformer will append the original size spec when present.
"""

# fmt: off
MSSQL_TO_POSTGRES: dict[str, str] = {
    # ── Exact numerics ────────────────────────────────────────────────────────
    "tinyint":          "smallint",       # MSSQL tinyint = 0-255; PG has no tinyint
    "smallint":         "smallint",
    "int":              "integer",
    "integer":          "integer",
    "bigint":           "bigint",
    "bit":              "boolean",
    "decimal":          "numeric",        # keep precision/scale from source
    "numeric":          "numeric",
    "money":            "numeric(19,4)",
    "smallmoney":       "numeric(10,4)",

    # ── Approximate numerics ─────────────────────────────────────────────────
    "float":            "double precision",
    "real":             "real",

    # ── Date & time ───────────────────────────────────────────────────────────
    "date":             "date",
    "time":             "time",
    "datetime":         "timestamp",          # no tz; use timestamptz if needed
    "datetime2":        "timestamptz",        # higher precision + tz-aware
    "smalldatetime":    "timestamp",
    "datetimeoffset":   "timestamptz",

    # ── Character strings ─────────────────────────────────────────────────────
    "char":             "char",               # keep length
    "varchar":          "varchar",            # keep length; MAX -> text
    "text":             "text",
    "nchar":            "char",               # Unicode -> UTF-8 in PG by default
    "nvarchar":         "varchar",            # keep length; MAX -> text
    "ntext":            "text",

    # ── Binary ────────────────────────────────────────────────────────────────
    "binary":           "bytea",
    "varbinary":        "bytea",
    "image":            "bytea",

    # ── Other ─────────────────────────────────────────────────────────────────
    "uniqueidentifier": "uuid",
    "xml":              "xml",
    "json":             "jsonb",
    "hierarchyid":      "text",               # no PG equivalent; store as ltree or text
    "geography":        "geometry",           # requires PostGIS
    "geometry":         "geometry",           # requires PostGIS
    "sql_variant":      "text",               # catch-all
    "rowversion":       "bytea",
    "timestamp":        "bytea",              # MSSQL timestamp ≠ datetime; it's rowversion
}
# fmt: on


def map_type(mssql_type: str, length: str | None = None,
             precision: str | None = None, scale: str | None = None) -> str:
    """
    Convert a single MSSQL column type to its PostgreSQL equivalent,
    preserving length / precision / scale where meaningful.

    Args:
        mssql_type: base type name, e.g. 'nvarchar', 'decimal'
        length:     character_maximum_length from information_schema (-1 = MAX)
        precision:  numeric_precision
        scale:      numeric_scale

    Returns:
        PostgreSQL type string, e.g. 'varchar(255)', 'numeric(18,4)'
    """
    base = mssql_type.strip().lower()
    pg = MSSQL_TO_POSTGRES.get(base, "text")  # default to text for unknowns

    # varchar/nvarchar/char/nchar: attach length
    if base in ("varchar", "nvarchar", "char", "nchar"):
        if length and int(length) == -1:
            return "text"            # MAX -> text
        if length:
            return f"{pg}({length})"
        return pg

    # decimal/numeric: attach precision + scale
    if base in ("decimal", "numeric"):
        if precision and scale:
            return f"numeric({precision},{scale})"
        if precision:
            return f"numeric({precision})"
        return "numeric"

    # float: MSSQL float(n) where n<=24 -> real, n>24 -> double precision
    if base == "float":
        if precision and int(precision) <= 24:
            return "real"
        return "double precision"

    return pg
