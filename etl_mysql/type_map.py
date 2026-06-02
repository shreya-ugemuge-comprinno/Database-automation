"""MSSQL -> MySQL data type mapping."""

# Types that pyodbc cannot fetch natively — cast to NVARCHAR during extraction
CAST_TYPES = {"geography", "geometry", "hierarchyid", "xml", "sql_variant"}

# Simple 1-to-1 mappings (lowercase base type -> MySQL type)
SIMPLE = {
    "tinyint":          "TINYINT UNSIGNED",   # MSSQL tinyint = 0-255
    "smallint":         "SMALLINT",
    "int":              "INT",
    "integer":          "INT",
    "bigint":           "BIGINT",
    "bit":              "TINYINT(1)",          # MySQL has no BOOLEAN, uses TINYINT(1)
    "money":            "DECIMAL(19,4)",
    "smallmoney":       "DECIMAL(10,4)",
    "float":            "DOUBLE",
    "real":             "FLOAT",
    "date":             "DATE",
    "time":             "TIME",
    "datetime":         "DATETIME",
    "datetime2":        "DATETIME(6)",         # microsecond precision
    "smalldatetime":    "DATETIME",
    "datetimeoffset":   "DATETIME(6)",         # MySQL has no tz-aware datetime
    "text":             "LONGTEXT",
    "ntext":            "LONGTEXT",
    "image":            "LONGBLOB",
    "binary":           "BLOB",
    "varbinary":        "LONGBLOB",
    "uniqueidentifier": "CHAR(36)",            # store UUID as string
    "xml":              "LONGTEXT",
    "geography":        "LONGTEXT",
    "geometry":         "LONGTEXT",
    "hierarchyid":      "VARCHAR(256)",
    "sql_variant":      "LONGTEXT",
    "rowversion":       "BIGINT UNSIGNED",
    "timestamp":        "BIGINT UNSIGNED",
}


def map_column_type(data_type: str, max_length: str | None,
                    precision: str | None, scale: str | None) -> str:
    """Return the MySQL type string for a given MSSQL column definition."""
    base = data_type.strip().lower()

    if base in ("varchar", "nvarchar"):
        if max_length and str(max_length) == "-1":
            return "LONGTEXT"
        if max_length and int(max_length) > 16383:
            return "MEDIUMTEXT"
        if max_length:
            return f"VARCHAR({max_length})"
        return "LONGTEXT"

    if base in ("char", "nchar"):
        if max_length and int(max_length) <= 255:
            return f"CHAR({max_length})"
        return "VARCHAR(255)"

    if base in ("decimal", "numeric"):
        if precision and scale:
            return f"DECIMAL({precision},{scale})"
        if precision:
            return f"DECIMAL({precision},0)"
        return "DECIMAL(18,4)"

    if base == "float":
        if precision and int(precision) <= 24:
            return "FLOAT"
        return "DOUBLE"

    return SIMPLE.get(base, "LONGTEXT")


def translate_default(default: str | None, mysql_type: str) -> str | None:
    """
    Translate a MSSQL DEFAULT expression to MySQL equivalent.
    Returns None to omit the DEFAULT clause.
    """
    if not default:
        return None

    d = default.strip()
    while d.startswith("(") and d.endswith(")"):
        d = d[1:-1].strip()

    low = d.lower()
    is_bool = "TINYINT(1)" in mysql_type

    # Date/time functions
    dt_map = {
        "getdate()":     "CURRENT_TIMESTAMP",
        "getdate":       "CURRENT_TIMESTAMP",
        "getutcdate()":  "CURRENT_TIMESTAMP",
        "getutcdate":    "CURRENT_TIMESTAMP",
        "sysdatetime()": "CURRENT_TIMESTAMP(6)",
        "sysdatetime":   "CURRENT_TIMESTAMP(6)",
        "sysdatetimeoffset()": "CURRENT_TIMESTAMP(6)",
        "sysutcdatetime()":    "CURRENT_TIMESTAMP",
        "sysutcdatetime":      "CURRENT_TIMESTAMP",
    }
    if low in dt_map:
        return dt_map[low]

    # UUID
    if low in ("newid()", "newid", "newsequentialid()", "newsequentialid"):
        return "(UUID())"

    # NULL
    if low == "null":
        return "NULL"

    # Empty strings
    if low in ("''", "n''"):
        return "''"

    # 0 / 1
    if low == "1":
        return "1"   # MySQL uses 1/0 for both bool and int
    if low == "0":
        return "0"

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
