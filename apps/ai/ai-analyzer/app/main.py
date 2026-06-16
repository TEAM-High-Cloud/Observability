import logging
import os

from fastapi import FastAPI, Request

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
)
log = logging.getLogger("ai-analyzer")

app = FastAPI(title="ai-analyzer", version="0.1.0")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    alerts = payload.get("alerts", [])
    log.info(
        "received %d alert(s); status=%s, groupKey=%s",
        len(alerts),
        payload.get("status"),
        payload.get("groupKey"),
    )
    return {"received": len(alerts)}
