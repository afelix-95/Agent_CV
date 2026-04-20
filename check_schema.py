import sys
sys.path.insert(0, 'src')
from agent_cv.config import settings
from psycopg import Connection
from psycopg.rows import dict_row

conn = Connection.connect(settings.postgres_dsn, row_factory=dict_row)
cur = conn.cursor()

# Get column info for document_versions table
cur.execute(
    '''select column_name, data_type from information_schema.columns 
       where table_name = 'document_versions' order by ordinal_position'''
)
columns = cur.fetchall()
print("=== document_versions columns ===")
for c in columns:
    print(f"  {c['column_name']}: {c['data_type']}")

cur.close()
conn.close()
