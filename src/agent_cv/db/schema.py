from pathlib import Path

from agent_cv.db.connection import get_connection


def apply_schema() -> None:
    sql_dir = Path(__file__).resolve().parents[3] / "sql"
    sql_files = sorted(sql_dir.glob("*.sql"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            for sql_file in sql_files:
                cur.execute(sql_file.read_text(encoding="utf-8"))
        conn.commit()
