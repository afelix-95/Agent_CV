import os
from pathlib import Path

from agent_cv.db.connection import get_connection


def _find_sql_dir() -> Path:
    # Explicit override (useful in tests or custom deployments)
    if env := os.environ.get("SQL_DIR"):
        return Path(env)
    # Installed in Docker image: sql/ is copied to /app/sql
    docker_path = Path("/app/sql")
    if docker_path.exists():
        return docker_path
    # Local development: sql/ lives at the project root (3 levels above src/agent_cv/db/)
    return Path(__file__).resolve().parents[4] / "sql"


def apply_schema() -> None:
    sql_dir = _find_sql_dir()
    sql_files = sorted(sql_dir.glob("*.sql"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            for sql_file in sql_files:
                cur.execute(sql_file.read_text(encoding="utf-8"))
        conn.commit()
