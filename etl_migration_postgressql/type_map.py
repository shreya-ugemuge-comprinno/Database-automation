"""MSSQL -> PostgreSQL data type mapping."""

# (base_type_lower) -> pg_type template
# Types that carry length/precision are handled in map_column_type()
SIMPLE = {
    "tinyint":           "smallint",
    "smallint":          "smallint",
    "int":               "integer",
    "integer":           "integer",
    "bigint":            "bigint",
    "bit":               "boolean",
    "money":             "numeric(19,4)",
    "smallmoney":        "numeric(10,4)",
    "float":             "double precision",
    "real":              "real",
    "date":              "date",
    "time":              "time",
    "datetime":          "timestamp",
    "datetime2":         "timestamptz",
    "smalldatetime":     "timestamp",
    "datetimeoffset":    "timestamptz",
    "text":              "text",
    "ntext":             "text",
    "image":             "bytea",
    "binary":            "bytea",
    "varbinary":         "bytea",
    "uniqueidentifier":  "uuid",
    "xml":               "text",
    "geography":         "text",
    "geometry":          "text",
    "hierarchyid":       "text",
    "sql_variant":       "text",
    "rowversion":        "bytea",
    "timestamp":         "bytea",
}

# Types that need special handling for CAST() during extraction
CAST_TYPES = {"geography", "geometry", "hierarchyid", "xml", "sql_variant"}


def map_column_type(data_type: str, max_length: str | None,
                    precision: str | None, scale: str | None) -> str:
    """Return the PostgreSQL type string for a given MSSQL column definition."""
    base = data_type.strip().lower()

    if base in ("varchar", "nvarchar", "char", "nchar"):
        if max_length and str(max_length) == "-1":
            return "text"
        if max_length:
            return f"varchar({max_length})"
        return "text"

    if base in ("decimal", "numeric"):
        if precision and scale:
            return f"numeric({precision},{scale})"
        if precision:
            return f"numeric({precision})"
        return "numeric"

    if base == "float":
        if precision and int(precision) <= 24:
            return "real"
        return "double precision"

    return SIMPLE.get(base, "text")


def translate_default(default: str | None, pg_type: str) -> str | None:
    """
    Translate a MSSQL DEFAULT expression to PostgreSQL.
    Returns None if the default should be omitted.
    """
    if not default:
        return None

    d = default.strip()
    # Strip all wrapping parens
    while d.startswith("(") and d.endswith(")"):
        d = d[1:-1].strip()

    low = d.lower()
    is_bool = pg_type.startswith("boolean")

    # Date/time functions
    dt_map = {
        "getdate()": "CURRENT_TIMESTAMP", "getdate": "CURRENT_TIMESTAMP",
        "getutcdate()": "NOW()", "getutcdate": "NOW()",
        "sysdatetime()": "CURRENT_TIMESTAMP", "sysdatetime": "CURRENT_TIMESTAMP",
        "sysdatetimeoffset()": "CURRENT_TIMESTAMP",
        "sysutcdatetime()": "NOW()", "sysutcdatetime": "NOW()",
    }
    if low in dt_map:
        return dt_map[low]

    # UUID
    if low in ("newid()", "newid", "newsequentialid()", "newsequentialid"):
        return "gen_random_uuid()"

    # NULL
    if low == "null":
        return "NULL"

    # Empty strings
    if low in ("''", "n''"):
        return "''"

    # 0 / 1
    if low == "1":
        return "true" if is_bool else "1"
    if low == "0":
        return "false" if is_bool else "0"

    # Numeric literals
    try:
        float(d)
        return d
    except ValueError:
        pass

    # Unicode string -> plain string
    if low.startswith("n'") and low.endswith("'"):
        return d[1:]

    # Quoted string
    if d.startswith("'") and d.endswith("'"):
        return d

    # Unknown — omit
    return None
