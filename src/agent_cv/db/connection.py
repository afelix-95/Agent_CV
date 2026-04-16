from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection
from psycopg.rows import dict_row

from agent_cv.config import settings


@contextmanager
def get_connection() -> Iterator[Connection]:
    conn = Connection.connect(settings.postgres_dsn, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()
