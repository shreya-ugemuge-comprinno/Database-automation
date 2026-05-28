#!/usr/bin/env python3
"""
etl_migrator — MSSQL -> PostgreSQL migration CLI

Commands:
  extract    Connect to MSSQL and extract DDL metadata + CSV data
  transform  Rewrite DDL to PostgreSQL syntax, clean CSVs
  audit      Remap schema identifiers, scan for incompatibilities
  load       Create schema in PostgreSQL and bulk-load data
  run-all    Run all four phases end-to-end

Schema remapping (--remap-schema):
  Pass  dbo=public  to replace every 'dbo.' reference with 'public.'
  Pass  dbo=myapp   to use a custom target schema name.
  Default is dbo=public.

Example (Windows CMD — use ^ for line continuation):
  python main.py run-all ^
    --src-host localhost --src-db AdventureWorks --src-user sa --src-pass secret ^
    --tgt-host localhost --tgt-db aw_pg        --tgt-user postgres --tgt-pass secret ^
    --schema dbo --remap-schema dbo=public --out C:\\migration
"""

import argparse
import json
import os
import sys
import tempfile

from utils.logger import get_logger
from extractor.mssql_extractor import MSSQLExtractor
from transformer.ddl_transformer import DDLTransformer
from auditor.schema_auditor import SchemaAuditor
from loader.postgres_loader import PostgresLoader

logger = get_logger("main")


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_remap(remap_str: str) -> tuple[str, str]:
    """Parse 'dbo=public' -> ('dbo', 'public')."""
    if "=" not in remap_str:
        raise argparse.ArgumentTypeError(
            f"--remap-schema must be in 'source=target' format, got: {remap_str!r}"
        )
    src, tgt = remap_str.split("=", 1)
    return src.strip(), tgt.strip()


# ── Phase handlers ────────────────────────────────────────────────────────────

def cmd_extract(args):
    logger.info("=== EXTRACT PHASE ===")
    extractor = MSSQLExtractor(
        host=args.src_host,
        port=args.src_port,
        database=args.src_db,
        user=getattr(args, "src_user", None),
        password=getattr(args, "src_pass", None),
        schema=args.schema,
        output_dir=args.out,
    )
    extractor.run(windows_auth=getattr(args, "windows_auth", False))
    logger.info("Extract complete -> %s", args.out)


def cmd_transform(args):
    logger.info("=== TRANSFORM PHASE ===")
    # Parse remap if available (run-all passes it; standalone transform uses default)
    remap = getattr(args, "remap_schema", "dbo=public")
    src_schema, tgt_schema = parse_remap(remap)
    transformer = DDLTransformer(
        input_dir=args.input,
        output_dir=args.out,
        target=args.target,
        source_schema=src_schema,
        target_schema=tgt_schema,
    )
    transformer.run()
    logger.info("Transform complete -> %s", args.out)


def cmd_audit(args):
    logger.info("=== AUDIT PHASE ===")
    src_schema, tgt_schema = parse_remap(args.remap_schema)

    # Load manifest from the transformed dir
    manifest_path = os.path.join(args.input, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    auditor = SchemaAuditor(
        input_dir=args.input,
        output_dir=args.out,
        source_schema=src_schema,
        target_schema=tgt_schema,
        fail_on_blockers=not args.ignore_blockers,
    )
    report = auditor.run(manifest)
    logger.info("Audit complete -> %s", args.out)
    return report


def cmd_load(args):
    logger.info("=== LOAD PHASE ===")
    loader = PostgresLoader(
        host=args.tgt_host,
        port=args.tgt_port,
        database=args.tgt_db,
        user=args.tgt_user,
        password=args.tgt_pass,
        input_dir=args.input,
    )
    loader.run()
    logger.info("Load complete.")


def cmd_run_all(args):
    """Run extract -> transform -> audit -> load end-to-end."""
    base = args.out or tempfile.mkdtemp(prefix="etl_migrator_")
    raw_dir         = os.path.join(base, "raw")
    transformed_dir = os.path.join(base, "transformed")
    audited_dir     = os.path.join(base, "audited")

    for d in (raw_dir, transformed_dir, audited_dir):
        os.makedirs(d, exist_ok=True)

    logger.info("Working directory: %s", base)

    # 1. Extract
    args.out = raw_dir
    cmd_extract(args)

    # 2. Transform
    args.input  = raw_dir
    args.out    = transformed_dir
    args.target = "postgres"
    cmd_transform(args)

    # 3. Audit
    args.input = transformed_dir
    args.out   = audited_dir
    cmd_audit(args)          # raises RuntimeError on blockers (unless --ignore-blockers)

    # 4. Load
    args.input = audited_dir
    cmd_load(args)


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog="etl_migrator",
        description="MSSQL -> PostgreSQL ETL migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── Shared arg groups ─────────────────────────────────────────────────────
    def add_src(p):
        p.add_argument("--src-host",  required=True,       help="MSSQL hostname/IP")
        p.add_argument("--src-port",  default=1433, type=int, help="MSSQL port (default 1433)")
        p.add_argument("--src-db",    required=True,       help="Source database name")
        p.add_argument("--src-user",  default=None,        help="MSSQL username (omit for Windows Auth)")
        p.add_argument("--src-pass",  default=None,        help="MSSQL password (omit for Windows Auth)")
        p.add_argument("--schema",    default="dbo",       help="Source schema filter (default: dbo). Use 'all' to extract every non-skipped schema")
        p.add_argument("--windows-auth", action="store_true", default=False,
                       help="Use Windows Authentication instead of SQL Server login (no --src-user/--src-pass needed)")

    def add_tgt(p):
        p.add_argument("--tgt-host",  required=True,       help="PostgreSQL hostname/IP")
        p.add_argument("--tgt-port",  default=5432, type=int, help="PostgreSQL port (default 5432)")
        p.add_argument("--tgt-db",    required=True,       help="Target database name")
        p.add_argument("--tgt-user",  required=True,       help="PostgreSQL username")
        p.add_argument("--tgt-pass",  required=True,       help="PostgreSQL password")

    def add_remap(p):
        p.add_argument(
            "--remap-schema",
            default="dbo=public",
            metavar="SRC=TGT",
            help="Schema remap rule (default: dbo=public). Example: dbo=myapp",
        )
        p.add_argument(
            "--ignore-blockers",
            action="store_true",
            default=False,
            help="Continue even if BLOCKER issues are found (not recommended for production)",
        )

    # ── extract ───────────────────────────────────────────────────────────────
    p_ext = sub.add_parser("extract", help="Extract DDL + CSV data from MSSQL")
    add_src(p_ext)
    p_ext.add_argument("--out", required=True, help="Output directory")

    # ── transform ─────────────────────────────────────────────────────────────
    p_tr = sub.add_parser("transform", help="Rewrite DDL to PostgreSQL syntax")
    p_tr.add_argument("--input",  required=True, help="Raw extract directory")
    p_tr.add_argument("--out",    required=True, help="Output directory")
    p_tr.add_argument("--target", default="postgres", choices=["postgres"])

    # ── audit ─────────────────────────────────────────────────────────────────
    p_au = sub.add_parser(
        "audit",
        help="Audit DDL for incompatibilities and remap schema identifiers",
    )
    p_au.add_argument("--input", required=True, help="Transformed directory")
    p_au.add_argument("--out",   required=True, help="Audited output directory")
    add_remap(p_au)

    # ── load ──────────────────────────────────────────────────────────────────
    p_ld = sub.add_parser("load", help="Load audited data into PostgreSQL")
    add_tgt(p_ld)
    p_ld.add_argument("--input", required=True, help="Audited directory")

    # ── run-all ───────────────────────────────────────────────────────────────
    p_all = sub.add_parser(
        "run-all",
        help="Run extract -> transform -> audit -> load in one shot",
    )
    add_src(p_all)
    add_tgt(p_all)
    add_remap(p_all)
    p_all.add_argument("--out", default=None, help="Working directory (default: temp dir)")

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "extract":   cmd_extract,
        "transform": cmd_transform,
        "audit":     cmd_audit,
        "load":      cmd_load,
        "run-all":   cmd_run_all,
    }

    try:
        dispatch[args.command](args)
        logger.info("Done.")
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        sys.exit(1)
    except RuntimeError as exc:
        # Audit blockers and other controlled failures
        logger.error("%s", exc)
        sys.exit(2)
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
