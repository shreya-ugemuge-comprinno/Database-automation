"""MongoDB BSON -> PostgreSQL data type mapping."""

from datetime import datetime, date
from decimal import Decimal
from bson import ObjectId, Decimal128, Int64, Binary

# BSON type -> PostgreSQL type
BSON_TO_PG = {
    "objectId":   "CHAR(24)",
    "string":     "TEXT",
    "int":        "INTEGER",
    "long":       "BIGINT",
    "double":     "DOUBLE PRECISION",
    "decimal":    "NUMERIC(38,18)",
    "bool":       "BOOLEAN",
    "date":       "TIMESTAMPTZ",
    "null":       "TEXT",
    "array":      "JSONB",
    "object":     "JSONB",
    "binData":    "BYTEA",
    "regex":      "TEXT",
}


def infer_pg_type(value) -> str:
    """Infer PostgreSQL column type from a Python/BSON value."""
    if value is None:
        return "TEXT"
    if isinstance(value, ObjectId):
        return "CHAR(24)"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        if -2147483648 <= value <= 2147483647:
            return "INTEGER"
        return "BIGINT"
    if isinstance(value, Int64):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    if isinstance(value, Decimal128):
        return "NUMERIC(38,18)"
    if isinstance(value, Decimal):
        return "NUMERIC(38,18)"
    if isinstance(value, datetime):
        return "TIMESTAMPTZ"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, bytes):
        return "BYTEA"
    if isinstance(value, Binary):
        return "BYTEA"
    if isinstance(value, (list, dict)):
        return "JSONB"
    if isinstance(value, str):
        return "TEXT"
    return "TEXT"


def widen_type(existing: str, new: str) -> str:
    """Given two PG types for the same column, return the wider one."""
    rank = {
        "BOOLEAN": 1, "INTEGER": 2, "BIGINT": 3,
        "DOUBLE PRECISION": 4, "NUMERIC(38,18)": 5,
        "DATE": 6, "TIMESTAMPTZ": 7,
        "CHAR(24)": 8, "TEXT": 9,
        "JSONB": 10, "BYTEA": 11,
    }
    r_e = rank.get(existing, 9)
    r_n = rank.get(new, 9)
    numeric = {"BOOLEAN", "INTEGER", "BIGINT", "DOUBLE PRECISION", "NUMERIC(38,18)"}
    if existing in numeric and new in numeric:
        return existing if r_e >= r_n else new
    if existing == new:
        return existing
    if existing == "TEXT" or new == "TEXT":
        return "TEXT"
    return existing if r_e >= r_n else new


def convert_for_pg(value):
    """Convert a BSON value to a PostgreSQL-compatible Python value."""
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, bool):
        return value
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
