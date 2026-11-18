"""Captive-portal web pages for users without the app.
CoovaChilli: uamserver = <base>/chilli.   MikroTik Hotspot: login.html redirects to <base>/mikrotik."""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import billing
from ..config import get_settings
from ..db import get_db
from ..models import Plan, WifiLogin
from ..otp import OtpError, issue_otp, normalize_phone, verify_otp
from ..radius import (authorize_user, chilli_logon_url, mikrotik_logon_url, private_ipv4, valid_chilli_params,
                      valid_mikrotik_params)

router = APIRouter(include_in_schema=False)
log = logging.getLogger("portal")
env = Environment(autoescape=select_autoescape(default=True))

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ name }}</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#eef2f6;
color:#1d2733;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:#fff;max-width:380px;width:100%;border-radius:14px;padding:28px 24px;box-shadow:0 6px 24px rgba(0,0,0,.08)}
h1{font-size:1.3rem;margin:0 0 4px}.sub{color:#5b6775;margin:0 0 20px;font-size:.95rem}
label{display:block;font-weight:600;margin:14px 0 6px;font-size:.9rem}
input[type=tel],input[type=text]{width:100%;font-size:1.1rem;padding:12px;border:1px solid #c8d1db;border-radius:8px}
.row{display:flex;align-items:center;gap:8px}.cc{padding:12px;background:#f3f5f8;border:1px solid #c8d1db;border-radius:8px}
button,.btn{display:block;text-align:center;text-decoration:none;width:100%;margin-top:18px;padding:13px;font-size:1rem;
font-weight:600;border:0;border-radius:8px;background:#0b6e4f;color:#fff}
.err{background:#fdecea;color:#a12622;padding:10px 12px;border-radius:8px;margin-top:12px;font-size:.9rem}
.ok{background:#e6f4ee;color:#0b6e4f;padding:12px;border-radius:8px;margin:0 0 12px;font-weight:600}
.demo{background:#fff6dd;border:1px dashed #d9a400;padding:10px 12px;border-radius:8px;margin-top:12px;font-size:.9rem}
.small{font-size:.8rem;color:#6b7785;margin-top:14px}a{color:#0b6e4f}
.chk{display:flex;gap:8px;align-items:flex-start;font-weight:400;font-size:.85rem}
</style></head><body><main class="card"><h1>{{ name }}</h1>{% block body %}{% endblock %}</main></body></html>"""

HIDDEN = """{% for k, v in gw.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}"""

PAGES = {
    "no_params": """<p class="sub">Please connect to the Wi-Fi network and open any website to reach this login page.</p>""",
    "phone": """<p class="sub">Enter your mobile number to get free internet.</p>
<form method="post" action="/portal/otp"><label for="phone">Mobile number</label>
<div class="row"><span class="cc">+91</span><input id="phone" name="phone" type="tel" inputmode="numeric" maxlength="10"
 pattern="[6-9][0-9]{9}" placeholder="98XXXXXXXX" required value="{{ phone or '' }}"></div>""" + HIDDEN + """
<label class="chk"><input type="checkbox" required> I agree to the terms of use of this Wi-Fi service.</label>
{% if error %}<div class="err">{{ error }}</div>{% endif %}<button type="submit">Get OTP</button></form>""",
    "otp": """<p class="sub">Enter the 6-digit OTP sent to +91 {{ phone }}.</p>
<form method="post" action="/portal/verify"><label for="otp">OTP</label>
<input id="otp" name="otp" type="text" inputmode="numeric" maxlength="6" pattern="[0-9]{6}" autocomplete="one-time-code"
 required autofocus><input type="hidden" name="request_id" value="{{ request_id }}">
<input type="hidden" name="phone" value="{{ phone }}">""" + HIDDEN + """
{% if demo_otp %}<div class="demo"><b>Demo mode:</b> no SMS is sent. Your OTP is <b>{{ demo_otp }}</b></div>{% endif %}
{% if error %}<div class="err">{{ error }}</div>{% endif %}<button type="submit">Verify &amp; Connect</button></form>
<p class="small"><a href="{{ again }}">Change number</a></p>""",
    "no_plan": """<div class="err">{{ error }}</div>
<p class="sub">Open the PM-WANI app to buy a data plan, then connect again.</p>
<p class="small"><a href="{{ again }}">Back</a></p>""",
    "connected": """<div class="ok">&#10003; You are connected to the internet.</div>
{% if userurl %}<a class="btn" href="{{ userurl }}">Continue browsing</a>{% endif %}
{% if logoff %}<p class="small"><a href="{{ logoff }}">Disconnect</a></p>{% endif %}""",
}
TEMPLATES = {k: env.from_string(BASE.replace("{% block body %}{% endblock %}", v)) for k, v in PAGES.items()}


def render(page: str, **ctx) -> HTMLResponse:
    return HTMLResponse(TEMPLATES[page].render(name=get_settings().portal_name, **ctx))


def gateway(src) -> dict | None:
    """Validated hotspot parameters from a CoovaChilli or MikroTik redirect (or our hidden form fields)."""
    if src.get("link_login"):  # MikroTik Hotspot
        gw = {k: (src.get(k) or "") for k in ("link_login", "chap_id", "chap_challenge", "mac", "ip", "nasid")}
        gw["dst"] = (src.get("dst") or "")[:512]
        return gw if valid_mikrotik_params(gw["link_login"], gw["chap_id"], gw["chap_challenge"], gw["mac"]) else None
    gw = {k: (src.get(k) or "") for k in ("uamip", "uamport", "challenge", "mac", "nasid")}
    gw["userurl"] = (src.get("userurl") or "")[:512]
    return gw if valid_chilli_params(gw["uamip"], gw["uamport"], gw["challenge"], gw["mac"]) else None


def again_url(gw: dict) -> str:
    from urllib.parse import urlencode
    if "link_login" in gw:
        return "/mikrotik?" + urlencode(gw)
    return "/chilli?" + urlencode({"res": "notyet", **gw})


def _web_url(url: str) -> str | None:
    return url if url.startswith(("http://", "https://")) else None


@router.get("/", response_class=HTMLResponse)
def root():
    return render("no_params")


@router.get("/chilli", response_class=HTMLResponse)
def chilli(request: Request):
    args = request.query_params
    res = args.get("res", "")
    log.info("chilli res=%s mac=%s nasid=%s ip=%s", res, args.get("mac"), args.get("nasid"), args.get("ip"))
    if res in ("success", "already"):
        uamip, uamport = args.get("uamip", ""), args.get("uamport", "")
        logoff = f"http://{uamip}:{uamport}/logoff" if private_ipv4(uamip) and uamport.isdigit() else None
        userurl = args.get("userurl", "")
        return render("connected", logoff=logoff,
                      userurl=userurl if userurl.startswith(("http://", "https://")) else None)
    gw = gateway(args)
    if not gw:
        return render("no_params")
    if res == "failed":
        reply = args.get("reply", "")
        return render("phone", gw=gw, error="Login failed. Please try again." + (f" ({reply})" if reply else ""))
    return render("phone", gw=gw)


@router.get("/mikrotik", response_class=HTMLResponse)
def mikrotik(request: Request):
    """MikroTik Hotspot entry point. The router's login.html redirects here with
    link_login, chap_id, chap_challenge (hex), mac, ip, nasid (router identity), dst, error."""
    args = request.query_params
    log.info("mikrotik mac=%s nasid=%s ip=%s error=%s", args.get("mac"), args.get("nasid"), args.get("ip"),
             args.get("error"))
    gw = gateway(args)
    if not gw:
        return render("no_params")
    error = args.get("error", "")
    return render("phone", gw=gw, error=f"Login failed. Please try again. ({error})" if error else None)


@router.post("/portal/otp", response_class=HTMLResponse)
async def portal_otp(request: Request, phone: str = Form(""), db: Session = Depends(get_db)):
    gw = gateway(await request.form())
    if not gw:
        return render("no_params")
    norm = normalize_phone(phone)
    if not norm:
        return render("phone", gw=gw, phone=phone, error="Enter a valid 10-digit mobile number.")
    try:
        issued = issue_otp(db, norm)
    except OtpError as e:
        return render("phone", gw=gw, phone=norm, error=e.message)
    return render("otp", gw=gw, phone=norm, request_id=issued.request_id, demo_otp=issued.demo_code,
                  again=again_url(gw))


@router.post("/portal/verify")
async def portal_verify(request: Request, request_id: str = Form(""), otp: str = Form(""), phone: str = Form(""),
                        db: Session = Depends(get_db)):
    gw = gateway(await request.form())
    if not gw:
        return render("no_params")
    try:
        user = verify_otp(db, request_id, otp)
    except OtpError as e:
        if e.code == "otp_invalid":
            return render("otp", gw=gw, phone=phone, request_id=request_id, error=e.message, again=again_url(gw))
        return render("phone", gw=gw, phone=phone, error=e.message)
    if not user.is_active:
        return render("no_plan", error="This number is blocked.", again=again_url(gw))

    sub, remaining = billing.current_subscription(db, user)
    if not sub and get_settings().free_login_on_portal:
        free = db.scalar(select(Plan).where(Plan.is_active, Plan.price_paise == 0).order_by(Plan.sort_order))
        if free:
            try:
                billing.claim_free_plan(db, user, free)
            except HTTPException:
                return render("no_plan", error="You have used today's free Wi-Fi.", again=again_url(gw))
            sub, remaining = billing.current_subscription(db, user)
    if not sub:
        return render("no_plan", error="You don't have an active data plan.", again=again_url(gw))

    password, _ = authorize_user(db, user, sub, remaining)
    db.add(WifiLogin(user_id=user.id, subscription_id=sub.id, nas_id=gw["nasid"] or None, client_mac=gw["mac"],
                     channel="portal", user_agent=request.headers.get("user-agent", "")[:255]))
    db.commit()
    if "link_login" in gw:
        url = mikrotik_logon_url(gw["link_login"], gw["chap_id"], gw["chap_challenge"], user.phone, password,
                                 _web_url(gw["dst"]))
    else:
        url = chilli_logon_url(gw["uamip"], gw["uamport"], gw["challenge"], user.phone, password,
                               _web_url(gw["userurl"]))
    return RedirectResponse(url, status_code=303)
