import sys
sys.path.insert(0, 'src')

from agent_cv.ingestion.ingest_service import ingest_documents

print("Starting full document reingest with language detection...")
result = ingest_documents(
    max_files=5000,
    force_reingest=True,
)
print(f"\nReingest complete: {result}")
