"""MongoDB BSON -> MySQL data type mapping."""

from datetime import datetime, date
from decimal import Decimal
from bson import ObjectId, Decimal128, Int64, Binary

# BSON type name -> MySQL type
BSON_TO_MYSQL = {
    "objectId":   "CHAR(24)",
    "string":     "LONGTEXT",
    "int":        "INT",
    "long":       "BIGINT",
    "double":     "DOUBLE",
    "decimal":    "DECIMAL(38,18)",
    "bool":       "TINYINT(1)",
    "date":       "DATETIME(6)",
    "null":       "LONGTEXT",
    "array":      "JSON",
    "object":     "JSON",
    "binData":    "LONGBLOB",
    "regex":      "VARCHAR(512)",
}


def infer_mysql_type(value) -> str:
    """Infer MySQL column type from a Python/BSON value."""
    if value is None:
        return "LONGTEXT"
    if isinstance(value, ObjectId):
        return "CHAR(24)"
    if isinstance(value, bool):
        return "TINYINT(1)"
    if isinstance(value, int):
        if -2147483648 <= value <= 2147483647:
            return "INT"
        return "BIGINT"
    if isinstance(value, Int64):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, Decimal128):
        return "DECIMAL(38,18)"
    if isinstance(value, Decimal):
        return "DECIMAL(38,18)"
    if isinstance(value, datetime):
        return "DATETIME(6)"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, bytes):
        return "LONGBLOB"
    if isinstance(value, Binary):
        return "LONGBLOB"
    if isinstance(value, (list, dict)):
        return "JSON"
    if isinstance(value, str):
        return "LONGTEXT"
    return "LONGTEXT"


def widen_type(existing: str, new: str) -> str:
    """Given two MySQL types for the same column, return the wider one."""
    rank = {
        "TINYINT(1)": 1, "INT": 2, "BIGINT": 3,
        "DOUBLE": 4, "DECIMAL(38,18)": 5,
        "DATE": 6, "DATETIME(6)": 7,
        "CHAR(24)": 8, "VARCHAR(512)": 9, "LONGTEXT": 10,
        "JSON": 11, "LONGBLOB": 12,
    }
    r_e = rank.get(existing, 10)
    r_n = rank.get(new, 10)
    # Numeric types widen within numeric; otherwise go LONGTEXT
    numeric = {"TINYINT(1)", "INT", "BIGINT", "DOUBLE", "DECIMAL(38,18)"}
    if existing in numeric and new in numeric:
        return existing if r_e >= r_n else new
    if existing == new:
        return existing
    # If one is text and other isn't, prefer LONGTEXT
    if existing == "LONGTEXT" or new == "LONGTEXT":
        return "LONGTEXT"
    # Same category keeps higher rank
    return existing if r_e >= r_n else new


def convert_for_mysql(value):
    """Convert a BSON value to a MySQL-compatible Python value."""
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, Int64):
        return int(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Binary):
        return bytes(value)
    if isinstance(value, (list, dict)):
        import json
        return json.dumps(value, default=str)
    if isinstance(value, (datetime, date)):
        return value
    return value
