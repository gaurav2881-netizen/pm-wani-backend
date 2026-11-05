"""PM-WANI PDOA / App Provider backend.  Run: uvicorn app.main:app"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, text

from .config import get_settings
from .db import Base, SessionLocal, engine
from .models import AccessPoint, Plan
from .routers import auth, consumer, pdo, pdoa, portal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_PLANS = [
    dict(name="Free Wi-Fi", description="30 minutes free every day", price_paise=0, validity_minutes=30,
         data_limit_mb=500, speed_down_kbps=2000, speed_up_kbps=1000, daily_claim_limit=1, sort_order=0),
    dict(name="Daily", description="1 GB for 24 hours", price_paise=1000, validity_minutes=24 * 60,
         data_limit_mb=1024, speed_down_kbps=10000, speed_up_kbps=5000, sort_order=1),
    dict(name="Weekly", description="5 GB for 7 days", price_paise=4900, validity_minutes=7 * 24 * 60,
         data_limit_mb=5 * 1024, speed_down_kbps=20000, speed_up_kbps=10000, sort_order=2),
    dict(name="Monthly", description="20 GB for 30 days", price_paise=14900, validity_minutes=30 * 24 * 60,
         data_limit_mb=20 * 1024, sort_order=3),
]


def init_db() -> None:
    with engine.connect() as conn:
        if not conn.execute(text("SELECT to_regclass('public.radacct')")).scalar():
            raise RuntimeError("FreeRADIUS tables are missing - import FreeRADIUS postgresql schema.sql first")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(Plan.id).limit(1)):
            db.add_all(Plan(**p) for p in DEFAULT_PLANS)
        if not db.scalar(select(AccessPoint.id).limit(1)):
            # the POC access point; radiusnasid/radiuscalled on the AP must be "pmwani-ap1"
            db.add(AccessPoint(name="POC Hotspot", nas_id="pmwani-ap1", ssid="PMWANI-Free", status="active"))
        db.commit()


app = FastAPI(
    title="PM-WANI Backend",
    version="1.0.0",
    description="API for the PM-WANI app (Consumer, PDO, PDOA) plus the CoovaChilli captive portal. "
                "Authenticate with POST /auth/otp/request + /auth/otp/verify, then send "
                "`Authorization: Bearer <access_token>`. Amounts are in paise (₹1 = 100).",
    docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
                   allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/healthz", include_in_schema=False, response_class=PlainTextResponse)
def healthz():
    return "ok"


API = "/api/v1"
app.include_router(auth.router, prefix=API)
app.include_router(consumer.router, prefix=API)
app.include_router(pdo.router, prefix=API)
app.include_router(pdoa.router, prefix=API)
app.include_router(portal.router)
