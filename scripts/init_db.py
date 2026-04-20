from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psycopg import connect, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_cv.config import settings
from agent_cv.db.schema import apply_schema


def _get_conninfo_parts() -> tuple[str, str]:
    conninfo = conninfo_to_dict(settings.postgres_dsn)
    target_db = conninfo.get("dbname") or conninfo.get("database")
    if not target_db:
        raise RuntimeError("POSTGRES_DSN must include a target database name.")

    # Use the maintenance DB for create-database checks.
    conninfo["dbname"] = "postgres"
    conninfo.pop("database", None)
    admin_conninfo = make_conninfo(**conninfo)
    return admin_conninfo, target_db


def ensure_database_exists() -> None:
    admin_conninfo, target_db = _get_conninfo_parts()

    with connect(admin_conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("select 1 from pg_database where datname = %s", (target_db,))
            if cur.fetchone() is None:
                cur.execute(sql.SQL("create database {}").format(sql.Identifier(target_db)))
                print(f"Database '{target_db}' created")
            else:
                print(f"Database '{target_db}' already exists")


if __name__ == "__main__":
    ensure_database_exists()
    apply_schema()
    print("Schema applied")
