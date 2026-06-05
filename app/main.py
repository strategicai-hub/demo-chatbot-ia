import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.services import rabbitmq, sai_sync
from app.webhook import router
from app.api import public_router as public_api_router
from app.api import router as api_router
from app.api_sai import router as sai_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # IMPORTANTE: o bot opera apenas em modo REATIVO (so responde a quem fala).
    # Os disparadores proativos (lembretes de evento e outreach em massa) estao
    # DESLIGADOS porque o envio em massa levou a Meta a bloquear o numero.
    # As funcoes reativas desses modulos (handle_reply / parse_presence_*) seguem
    # disponiveis pelo consumer; so os loops de disparo foram removidos.
    #   reminder_task = asyncio.create_task(event_reminders.start_loop())
    #   outreach_task = asyncio.create_task(outreach.start_loop())
    sync_task = asyncio.create_task(sai_sync.start_polling())
    try:
        yield
    finally:
        sync_task.cancel()
        try:
            await sync_task
        except (asyncio.CancelledError, Exception):
            pass
        await rabbitmq.close()


app = FastAPI(title=f"{settings.BUSINESS_NAME} - API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(public_api_router)
app.include_router(api_router)
# Traefik forwards /demo-chatbot-ia/... sem strip — montamos sai_router sob
# WEBHOOK_PATH para que POST {baseUrl}/sai/bind e /sai/config cheguem aqui.
app.include_router(sai_router, prefix=settings.WEBHOOK_PATH)


@app.get("/health")
async def health():
    return {"status": "ok"}
