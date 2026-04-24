import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_cv.api.routes import router
from agent_cv.ingestion.sharepoint_watcher import get_sharepoint_watcher, sharepoint_configured
from agent_cv.services.graph_service import graph_configured
from agent_cv.teams.agent import get_graph_bot

# Forward all agent_cv loggers through uvicorn's handler so messages appear in
# the same terminal output as the server INFO lines.
logging.getLogger("agent_cv").handlers = logging.getLogger("uvicorn").handlers
logging.getLogger("agent_cv").setLevel(logging.DEBUG)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if graph_configured():
        get_graph_bot().start()
    if sharepoint_configured():
        get_sharepoint_watcher().start()
    yield
    await get_graph_bot().stop()
    if sharepoint_configured():
        await get_sharepoint_watcher().stop()


app = FastAPI(title="Agent CV Service", version="0.1.0", lifespan=lifespan)
app.include_router(router)
