"""MSSQL -> MongoDB data type mapping and value conversion."""

from datetime import datetime, date, time
from decimal import Decimal
from uuid import UUID

# Types that pyodbc cannot fetch natively — cast to NVARCHAR during extraction
CAST_TYPES = {"geography", "geometry", "hierarchyid", "xml", "sql_variant"}

# MSSQL type -> MongoDB BSON conceptual type (used for reporting/validation)
BSON_MAP = {
    "tinyint":          "int",
    "smallint":         "int",
    "int":              "int",
    "integer":          "int",
    "bigint":           "long",
    "bit":              "bool",
    "money":            "decimal128",
    "smallmoney":       "decimal128",
    "float":            "double",
    "real":             "double",
    "decimal":          "decimal128",
    "numeric":          "decimal128",
    "date":             "date",
    "time":             "string",
    "datetime":         "date",
    "datetime2":        "date",
    "smalldatetime":    "date",
    "datetimeoffset":   "date",
    "char":             "string",
    "nchar":            "string",
    "varchar":          "string",
    "nvarchar":         "string",
    "text":             "string",
    "ntext":            "string",
    "binary":           "binData",
    "varbinary":        "binData",
    "image":            "binData",
    "uniqueidentifier": "string",
    "xml":              "string",
    "geography":        "string",
    "geometry":         "string",
    "hierarchyid":      "string",
    "sql_variant":      "string",
    "rowversion":       "long",
    "timestamp":        "long",
}


def get_bson_type(data_type: str) -> str:
    """Return the BSON type name for a given MSSQL data type."""
    return BSON_MAP.get(data_type.strip().lower(), "string")


def convert_value(value, mssql_type: str):
    """Convert a Python value from pyodbc to a MongoDB-compatible value."""
    if value is None:
        return None

    base = mssql_type.strip().lower()

    if base == "bit":
        if isinstance(value, bool):
            return value
        return bool(int(value))

    if base in ("tinyint", "smallint", "int", "integer"):
        return int(value)

    if base == "bigint":
        return int(value)

    if base in ("float", "real"):
        return float(value)

    if base in ("decimal", "numeric", "money", "smallmoney"):
        # Store as string to preserve precision (Decimal128 via pymongo)
        from bson.decimal128 import Decimal128
        if isinstance(value, Decimal):
            return Decimal128(value)
        try:
            return Decimal128(Decimal(str(value)))
        except Exception:
            return str(value)

    if base in ("datetime", "datetime2", "smalldatetime", "datetimeoffset"):
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        return value

    if base == "date":
        if isinstance(value, (datetime, date)):
            return datetime(value.year, value.month, value.day)
        return value

    if base == "time":
        if isinstance(value, time):
            return str(value)
        return str(value)

    if base in ("binary", "varbinary", "image"):
        if isinstance(value, (bytes, bytearray)):
            return value
        return value

    if base == "uniqueidentifier":
        if isinstance(value, UUID):
            return str(value)
        return str(value)

    if base in CAST_TYPES:
        return str(value)

    return value
