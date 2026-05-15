import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent_cv import __version__
from agent_cv.api.routes import router
from agent_cv.db.schema import apply_schema
from agent_cv.config import owv_configured
from agent_cv.ingestion.owv_sync_service import get_owv_sync_service
from agent_cv.ingestion.sharepoint_watcher import get_sharepoint_watcher, sharepoint_configured
from agent_cv.services.graph_service import graph_configured
from agent_cv.teams.agent import get_teams_bot

logger = logging.getLogger("agent_cv")

# Forward all agent_cv loggers through uvicorn's handler so messages appear in
# the same terminal output as the server INFO lines.
logging.getLogger("agent_cv").handlers = logging.getLogger("uvicorn").handlers
logging.getLogger("agent_cv").setLevel(logging.DEBUG)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Applying database schema migrations...")
    apply_schema()
    logger.info("Schema up to date.")
    if graph_configured():
        await get_teams_bot().start()
    if sharepoint_configured():
        get_sharepoint_watcher().start()
    if owv_configured():
        get_owv_sync_service().start()
    yield
    await get_teams_bot().stop()
    if sharepoint_configured():
        await get_sharepoint_watcher().stop()
    if owv_configured():
        await get_owv_sync_service().stop()


app = FastAPI(title="Agent CV Service", version=__version__, lifespan=lifespan)
app.include_router(router)
