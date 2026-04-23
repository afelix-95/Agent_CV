from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_cv.api.routes import router
from agent_cv.services.graph_service import graph_configured
from agent_cv.teams.agent import get_graph_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    if graph_configured():
        get_graph_bot().start()
    yield
    await get_graph_bot().stop()


app = FastAPI(title="Agent CV Service", version="0.1.0", lifespan=lifespan)
app.include_router(router)
