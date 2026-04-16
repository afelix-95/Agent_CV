from fastapi import FastAPI

from agent_cv.api.routes import router

app = FastAPI(title="Agent CV Service", version="0.1.0")
app.include_router(router)
