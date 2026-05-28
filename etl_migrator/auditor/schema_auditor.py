"""
Audit & Transform Phase
────────────────────────
Runs BEFORE the load phase as a safety gate.

Responsibilities:
1. Remap schema identifiers (e.g. dbo -> public) across all DDL.
2. Scan views, procedures, functions for MSSQL-specific constructs.
3. Classify every finding as BLOCKER / WARNING / INFO.
4. Write compatibility_report.json and a diff of what changed.
5. Halt the pipeline if any BLOCKERs are found.

Output files (written to output_dir):
  • schema.sql                — DDL with schema remapping applied
  • compatibility_report.json — structured findings per object
  • schema.diff               — unified diff of original vs remapped DDL
"""

import difflib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Literal

from utils.logger import get_logger

logger = get_logger("auditor")

# ── Severity type ─────────────────────────────────────────────────────────────

Severity = Literal["BLOCKER", "WARNING", "INFO"]

# ── Audit rules catalogue ─────────────────────────────────────────────────────
# Each rule: (regex_pattern, severity, short_code, human_message)

AUDIT_RULES: list[tuple[str, Severity, str, str]] = [
    # BLOCKERs — cannot migrate without manual rewrite
    (
        r"\bEXEC\s*\(",
        "BLOCKER", "DYNAMIC_SQL_EXEC",
        "Dynamic SQL via EXEC() — must be rewritten as PL/pgSQL EXECUTE '...' USING",
    ),
    (
        r"#\w+",
        "BLOCKER", "TEMP_TABLE_HASH",
        "Temp table (#table) — use CREATE TEMP TABLE or CTEs in PostgreSQL",
    ),
    (
        r"\bOPENROWSET\b",
        "BLOCKER", "OPENROWSET",
        "OPENROWSET is MSSQL-specific — use postgres_fdw or ETL instead",
    ),
    (
        r"\bOPENQUERY\b",
        "BLOCKER", "OPENQUERY",
        "OPENQUERY is MSSQL-specific — use postgres_fdw or dblink instead",
    ),
    (
        r"\bBULK\s+INSERT\b",
        "BLOCKER", "BULK_INSERT",
        "BULK INSERT is MSSQL-specific — use COPY or pg_bulkload in PostgreSQL",
    ),
    (
        r"\bSP_EXECUTESQL\b",
        "BLOCKER", "SP_EXECUTESQL",
        "sp_executesql is MSSQL-specific — rewrite as PL/pgSQL EXECUTE ... USING",
    ),
    (
        r"\bCURSOR\b",
        "BLOCKER", "CURSOR",
        "CURSOR syntax differs significantly — review and rewrite for PL/pgSQL",
    ),
    (
        r"\bFOR\s+XML\b",
        "BLOCKER", "FOR_XML",
        "FOR XML is MSSQL-specific — use xmlelement()/xmlforest() or json_agg() in PostgreSQL",
    ),

    # WARNINGs — auto-fixable or need verification
    (
        r"\bTOP\s+\d+\b",
        "WARNING", "TOP_N",
        "TOP n — replace with LIMIT n (or FETCH FIRST n ROWS ONLY)",
    ),
    (
        r"\bISNULL\s*\(",
        "WARNING", "ISNULL",
        "ISNULL(x,y) — replace with COALESCE(x,y)",
    ),
    (
        r"\bIIF\s*\(",
        "WARNING", "IIF",
        "IIF(cond,t,f) — replace with CASE WHEN cond THEN t ELSE f END",
    ),
    (
        r"@@ROWCOUNT",
        "WARNING", "ROWCOUNT",
        "@@ROWCOUNT — use GET DIAGNOSTICS row_count = ROW_COUNT in PL/pgSQL",
    ),
    (
        r"@@IDENTITY",
        "WARNING", "IDENTITY",
        "@@IDENTITY — use LASTVAL() or RETURNING clause in PostgreSQL",
    ),
    (
        r"\bRAISERROR\b",
        "WARNING", "RAISERROR",
        "RAISERROR — replace with RAISE EXCEPTION '...' in PL/pgSQL",
    ),
    (
        r"\bTHROW\b",
        "WARNING", "THROW",
        "THROW — replace with RAISE in PL/pgSQL",
    ),
    (
        r"\bDATEADD\s*\(",
        "WARNING", "DATEADD",
        "DATEADD(unit,n,d) — replace with d + INTERVAL '...' or d + (n || ' unit')::interval",
    ),
    (
        r"\bDATEDIFF\s*\(",
        "WARNING", "DATEDIFF",
        "DATEDIFF(unit,a,b) — use EXTRACT(EPOCH FROM (b-a)) or date_part() in PostgreSQL",
    ),
    (
        r"\bCONVERT\s*\(",
        "WARNING", "CONVERT",
        "CONVERT(type,expr) — replace with CAST(expr AS type) or type::expr",
    ),
    (
        r"\bSTRING_AGG\s*\(",
        "WARNING", "STRING_AGG_ORDER",
        "STRING_AGG with WITHIN GROUP — PostgreSQL STRING_AGG uses ORDER BY inside aggregate instead",
    ),
    (
        r"\bWITH\s*\(\s*NOLOCK\s*\)",
        "WARNING", "NOLOCK",
        "WITH (NOLOCK) hint — remove entirely; PostgreSQL uses MVCC and has no equivalent",
    ),
    (
        r"\bNOLOCK\b",
        "WARNING", "NOLOCK_BARE",
        "NOLOCK hint — remove; not applicable in PostgreSQL",
    ),

    # INFO — informational, usually auto-handled by type_mapping.py
    (
        r"\bSET\s+NOCOUNT\s+ON\b",
        "INFO", "SET_NOCOUNT",
        "SET NOCOUNT ON — not needed in PostgreSQL, safe to remove",
    ),
    (
        r"\bGETDATE\s*\(\)",
        "INFO", "GETDATE",
        "GETDATE() — replaced with NOW() or CURRENT_TIMESTAMP",
    ),
    (
        r"\bGETUTCDATE\s*\(\)",
        "INFO", "GETUTCDATE",
        "GETUTCDATE() — replaced with NOW() AT TIME ZONE 'UTC'",
    ),
    (
        r"\bNEWID\s*\(\)",
        "INFO", "NEWID",
        "NEWID() — replaced with gen_random_uuid() (requires pgcrypto extension)",
    ),
    (
        r"\bNVARCHAR\b",
        "INFO", "NVARCHAR",
        "NVARCHAR — auto-mapped to VARCHAR; PostgreSQL is UTF-8 by default",
    ),
    (
        r"\bDATETIME2\b",
        "INFO", "DATETIME2",
        "DATETIME2 — auto-mapped to TIMESTAMPTZ",
    ),
    (
        r"\bUNIQUEIDENTIFIER\b",
        "INFO", "UNIQUEIDENTIFIER",
        "UNIQUEIDENTIFIER — auto-mapped to UUID",
    ),
]


# ── Finding dataclass ─────────────────────────────────────────────────────────

@dataclass
class Finding:
    object_name: str
    object_type: str          # TABLE | VIEW | PROCEDURE | FUNCTION | INDEX
    severity: Severity
    code: str
    message: str
    line_number: int | None = None
    snippet: str | None = None


# ── Schema remapper ───────────────────────────────────────────────────────────

class SchemaRemapper:
    """
    Rewrites all occurrences of the source schema qualifier in a SQL string
    to the target schema.

    Handles all three MSSQL quoting styles:
      [dbo].[TableName]   ->  public.TableName
      "dbo"."TableName"   ->  public.TableName
      dbo.TableName       ->  public.TableName

    Skips occurrences inside single-quoted string literals so we don't
    corrupt data literals like 'prefix dbo.tablename suffix'.
    """

    def __init__(self, source_schema: str = "dbo", target_schema: str = "public"):
        self.source = source_schema
        self.target = target_schema

        s = re.escape(source_schema)
        self._patterns = [
            # [dbo]. style
            re.compile(rf'\[{s}\]\.', re.IGNORECASE),
            # "dbo". style
            re.compile(rf'"{s}"\.', re.IGNORECASE),
            # bare dbo. style (only before an identifier or [)
            re.compile(rf'\b{s}\.(?=[a-zA-Z_\[])', re.IGNORECASE),
        ]

    def remap(self, sql: str) -> str:
        """Apply schema remapping, skipping content inside string literals."""
        # Split on single-quoted literals to avoid remapping inside them
        # Tokens alternate: [outside, inside, outside, inside, ...]
        tokens = re.split(r"('(?:[^']|'')*')", sql)
        result = []
        for i, token in enumerate(tokens):
            if i % 2 == 1:
                # Inside a string literal — leave untouched
                result.append(token)
            else:
                # Outside literal — apply all patterns
                t = token
                for pat in self._patterns:
                    t = pat.sub(f"{self.target}.", t)
                result.append(t)
        return "".join(result)

    def count_occurrences(self, sql: str) -> int:
        """Count how many schema references were found (for reporting)."""
        total = 0
        for pat in self._patterns:
            total += len(pat.findall(sql))
        return total


# ── Auditor ───────────────────────────────────────────────────────────────────

class SchemaAuditor:
    def __init__(
        self,
        input_dir: str,
        output_dir: str,
        source_schema: str = "dbo",
        target_schema: str = "public",
        fail_on_blockers: bool = True,
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.source_schema = source_schema
        self.target_schema = target_schema
        self.fail_on_blockers = fail_on_blockers
        self.remapper = SchemaRemapper(source_schema, target_schema)
        self.findings: list[Finding] = []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scan_sql(self, sql: str, object_name: str, object_type: str):
        """Run all audit rules against a SQL string and collect findings."""
        lines = sql.splitlines()
        for pattern, severity, code, message in AUDIT_RULES:
            for lineno, line in enumerate(lines, start=1):
                # Skip comment lines
                stripped = line.strip()
                if stripped.startswith("--") or stripped.startswith("/*"):
                    continue
                if re.search(pattern, line, re.IGNORECASE):
                    self.findings.append(Finding(
                        object_name=object_name,
                        object_type=object_type,
                        severity=severity,
                        code=code,
                        message=message,
                        line_number=lineno,
                        snippet=line.strip()[:120],
                    ))

    def _check_dynamic_schema_in_strings(self, sql: str, object_name: str, object_type: str):
        """
        Flag hardcoded schema references inside string literals
        (dynamic SQL built by concatenation) — these can't be auto-remapped.
        """
        # Find string literals
        literals = re.findall(r"'(?:[^']|'')*'", sql)
        for lit in literals:
            if re.search(rf'\b{re.escape(self.source_schema)}\.', lit, re.IGNORECASE):
                self.findings.append(Finding(
                    object_name=object_name,
                    object_type=object_type,
                    severity="BLOCKER",
                    code="HARDCODED_SCHEMA_IN_STRING",
                    message=(
                        f"Hardcoded '{self.source_schema}.' found inside a string literal — "
                        f"dynamic SQL schema reference cannot be auto-remapped. Manual fix required."
                    ),
                    snippet=lit[:120],
                ))

    # ── Audit DDL (schema.sql) ────────────────────────────────────────────────

    def audit_and_remap_ddl(self) -> tuple[str, str]:
        """
        Read schema.sql, apply schema remapping, scan for issues.
        Returns (original_sql, remapped_sql).
        """
        sql_path = os.path.join(self.input_dir, "schema.sql")
        if not os.path.exists(sql_path):
            raise FileNotFoundError(f"schema.sql not found in {self.input_dir}")

        with open(sql_path, encoding="utf-8") as f:
            original = f.read()

        remapped = self.remapper.remap(original)
        ref_count = self.remapper.count_occurrences(original)

        logger.info(
            "Schema remapping: '%s' -> '%s'  (%d reference(s) rewritten)",
            self.source_schema, self.target_schema, ref_count,
        )

        # Scan the remapped DDL for remaining MSSQL constructs
        self._scan_sql(remapped, "schema.sql", "DDL")

        return original, remapped

    # ── Audit manifest objects ────────────────────────────────────────────────

    def audit_manifest(self, manifest: list[dict]):
        """
        Scan the manifest for any stored SQL in views/procedures/functions.
        Also checks for dynamic schema refs inside string literals.
        """
        for table in manifest:
            obj_name = f"{table.get('schema', '')}.{table.get('name', '')}"

            # Scan any raw_sql field if extractor captured view/proc bodies
            for key in ("view_definition", "procedure_body", "function_body"):
                sql_body = table.get(key)
                if sql_body:
                    obj_type = key.replace("_", " ").replace("body", "").strip().upper()
                    remapped_body = self.remapper.remap(sql_body)
                    self._scan_sql(remapped_body, obj_name, obj_type)
                    self._check_dynamic_schema_in_strings(sql_body, obj_name, obj_type)
                    # Write remapped body back
                    table[key] = remapped_body

    # ── Reporting ─────────────────────────────────────────────────────────────

    def write_report(self):
        """Write compatibility_report.json with all findings grouped by severity."""
        blockers  = [f for f in self.findings if f.severity == "BLOCKER"]
        warnings  = [f for f in self.findings if f.severity == "WARNING"]
        infos     = [f for f in self.findings if f.severity == "INFO"]

        report = {
            "summary": {
                "source_schema":  self.source_schema,
                "target_schema":  self.target_schema,
                "total_findings": len(self.findings),
                "blockers":       len(blockers),
                "warnings":       len(warnings),
                "info":           len(infos),
                "gate":           "FAIL" if blockers else "PASS",
            },
            "blockers":  [asdict(f) for f in blockers],
            "warnings":  [asdict(f) for f in warnings],
            "info":      [asdict(f) for f in infos],
        }

        report_path = os.path.join(self.output_dir, "compatibility_report.json")
        with open(report_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, indent=2)

        logger.info("Compatibility report: %s", report_path)
        logger.info(
            "Audit gate: %s  (blockers=%d  warnings=%d  info=%d)",
            report["summary"]["gate"],
            len(blockers), len(warnings), len(infos),
        )

        if blockers:
            logger.error("=" * 60)
            logger.error("PIPELINE HALTED — %d BLOCKER(s) found:", len(blockers))
            for b in blockers:
                logger.error("  [%s] %s (line %s): %s", b.object_name, b.code, b.line_number, b.message)
            logger.error("Fix the above issues and re-run.")
            logger.error("=" * 60)

        return report

    def write_diff(self, original: str, remapped: str):
        """Write a unified diff of original schema.sql vs remapped version."""
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            remapped.splitlines(keepends=True),
            fromfile=f"schema.sql (original — {self.source_schema})",
            tofile=f"schema.sql (remapped — {self.target_schema})",
            lineterm="",
        )
        diff_text = "".join(diff)
        diff_path = os.path.join(self.output_dir, "schema.diff")
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(diff_text if diff_text else "-- No differences (schema names already match)\n")
        logger.info("Schema diff written: %s", diff_path)

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self, manifest: list[dict]) -> dict:
        """
        Full audit run. Returns the report dict.
        Raises RuntimeError if fail_on_blockers=True and blockers are found.
        """
        os.makedirs(self.output_dir, exist_ok=True)

        logger.info("=== AUDIT & TRANSFORM PHASE ===")
        logger.info(
            "Schema remap: '%s' -> '%s'", self.source_schema, self.target_schema
        )

        # 1. Audit + remap DDL
        original_sql, remapped_sql = self.audit_and_remap_ddl()

        # 2. Write remapped schema.sql (overwrites transformer output)
        schema_out = os.path.join(self.output_dir, "schema.sql")
        with open(schema_out, "w", encoding="utf-8") as f:
            f.write(remapped_sql)

        # 3. Audit manifest (views, procs, funcs)
        self.audit_manifest(manifest)

        # 4. Update manifest schema names
        if self.source_schema != self.target_schema:
            for table in manifest:
                if table.get("schema", "").lower() == self.source_schema.lower():
                    table["schema"] = self.target_schema

        # 5. Write manifest
        manifest_path = os.path.join(self.output_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # 6. Write diff
        self.write_diff(original_sql, remapped_sql)

        # 7. Write report + check gate
        report = self.write_report()

        if self.fail_on_blockers and report["summary"]["blockers"] > 0:
            raise RuntimeError(
                f"Audit failed: {report['summary']['blockers']} BLOCKER(s) found. "
                f"See compatibility_report.json for details."
            )

        return report
