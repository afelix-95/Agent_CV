from pathlib import Path

from agent_cv.db.connection import get_connection


def apply_schema() -> None:
    schema_path = Path(__file__).resolve().parents[3] / "sql" / "001_init.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
