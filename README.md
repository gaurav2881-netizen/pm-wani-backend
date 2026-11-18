# PM-WANI Backend

FastAPI + PostgreSQL backend for the PM-WANI app (Consumer, PDO, PDOA roles) and the CoovaChilli captive portal.
FreeRADIUS reads the same PostgreSQL database, so a plan bought in the app directly controls the user's
session time, data limit and speed on the hotspot.

```
Flutter app ──HTTP/JSON──> nginx :80 ──> FastAPI :8000 ──> PostgreSQL "pmwani" <── FreeRADIUS <── CoovaChilli (APs)
Phone without app ──> CoovaChilli redirect ──> /chilli (portal pages, same backend)
```

## Deploy on EC2

1. Copy this folder to the server (git clone, or `scp` the zip and `unzip`).
2. Run:
   ```bash
   cd pmwani-backend
   sudo PDOA_PHONES=<your 10-digit admin mobile> bash deploy/install.sh
   ```
3. Open `http://<EC2-IP>/docs` — interactive API documentation (try every endpoint from the browser).
4. Each AP's `radiusnasid` (in `/etc/config/chilli`) must equal its `nas_id` in the backend. The POC AP
   already uses `pmwani-ap1`. FreeRADIUS copies it into Called-Station-Id, which links sessions to hotspots.

What the installer does: installs PostgreSQL, creates database `pmwani`, imports the FreeRADIUS schema,
switches the FreeRADIUS `sql` module from MariaDB to PostgreSQL (previous config saved as
`mods-available/sql.before-postgres`), installs the API as the `pmwani-api` service, points nginx at it, and
stops the old POC portal. It is safe to re-run after code changes (secrets are kept).

Settings: `/etc/pmwani-api.env` (restart after editing: `sudo systemctl restart pmwani-api`).
Logs: `sudo journalctl -u pmwani-api -f`.

| Setting | Meaning |
|---|---|
| `PDOA_PHONES` | Numbers that become PDOA admins on login (comma separated) |
| `OTP_DEMO=true` | OTP is returned in the API response (`demo_otp`) and shown on the portal; no SMS |
| `SMS_PROVIDER=msg91`, `MSG91_AUTHKEY`, `MSG91_TEMPLATE_ID` | Real SMS via MSG91 with a DLT-approved template containing `##otp##` (check the request format against MSG91's current docs before going live). Set `OTP_DEMO=false`. |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Empty = mock payments. Use test keys first, then live. |
| `RAZORPAY_WEBHOOK_SECRET` | For `/api/v1/payments/razorpay/webhook` (events `payment.captured`, `order.paid`). Webhooks need HTTPS (domain). |
| `CHILLI_UAMSECRET` | Must equal `uamsecret` in `/etc/config/chilli` on every AP |
| `PUBLIC_BASE_URL` | Change to `https://api.yourdomain` once the domain + HTTPS is ready |

## MikroTik Hotspot sites

MikroTik routers use their built-in Hotspot with RADIUS (no CoovaChilli):
- The router's `login.html` (published by the installer at `/files/mikrotik/login.html`) redirects users to
  `<PUBLIC_BASE_URL>/mikrotik` with the hotspot details; after OTP the portal sends the browser back to the
  router's `/login` with an HTTP-CHAP password.
- RADIUS replies include `Mikrotik-Rate-Limit` (upload/download) and `Mikrotik-Total-Limit(-Gigawords)` besides
  the CoovaChilli attributes; each router ignores the other vendor's attributes.
- The router's **system identity** is sent as NAS-Identifier and must equal the hotspot's `nas_id`.
- In-app login: `POST /api/v1/wifi/login` also accepts `link_login, chap_id, chap_challenge, mac, nasid, dst`
  (from the `/mikrotik?...` redirect URL).

## API (base: `/api/v1`, auth: `Authorization: Bearer <token>`)

Amounts are in **paise** (₹1 = 100). Data in **bytes** (plans use MB). Errors return `{"detail": "<code>"}`.

| Method & path | Who | Purpose |
|---|---|---|
| `POST /auth/otp/request` `{phone}` | all | Send OTP → `{request_id, expires_in, demo_otp}` |
| `POST /auth/otp/verify` `{request_id, otp}` | all | → `{access_token, user}` (user created on first login) |
| `GET/PATCH /me` | user | Profile (name, email, role, kyc_status, pdo_status) |
| `POST /me/kyc` | user | KYC submission (stores doc type + last 4 digits only) |
| `POST /me/pdo-application` | consumer | Apply to become a PDO |
| `GET /hotspots?lat=&lng=&radius_km=` | all | Active hotspots, nearest first; `online` = user activity in last 15 min |
| `GET /plans` | all | Active plans |
| `POST /plans/{id}/buy` `{method}` | user | `free` (₹0 plans, limited per 24h), `wallet`, or `gateway` (returns an order) |
| `GET /subscriptions` | user | User's plans (purchases during a running plan are queued after it) |
| `GET /wallet` | user | Balance + transactions |
| `POST /wallet/topup` `{amount_paise}` | user | Create payment order |
| `POST /payments/verify` | user | After Razorpay Checkout (`razorpay_signature="mock"` in mock mode) |
| `POST /wifi/login` | user | In-app hotspot login → `{logon_url, session_timeout, remaining_bytes, plan}`; 402 `no_active_plan` |
| `GET /wifi/status` | user | Current plan, data/time left, live session |
| `GET /sessions` | user | Session history (from RADIUS accounting) |
| `GET/POST /pdo/access-points` | PDO | List / register APs (new APs are `pending`; response includes CoovaChilli settings) |
| `GET /pdo/access-points/{id}/config` | PDO | CoovaChilli settings for an AP |
| `GET /pdo/stats?days=30` | PDO | Sessions, users, data, minutes per AP |
| `GET /pdoa/dashboard` | PDOA | Users, PDOs, APs, sessions/data/revenue today |
| `GET /pdoa/users?role=&pdo_status=&phone=` | PDOA | Users |
| `POST /pdoa/pdo-applications/{user_id}/decision` `{approve}` | PDOA | Approve/reject PDO |
| `POST /pdoa/users/{user_id}/kyc` `{approve}` | PDOA | Verify/reject KYC |
| `POST /pdoa/users/{user_id}/active` `{approve}` | PDOA | Block/unblock user (removes RADIUS access) |
| `GET /pdoa/access-points?status=` / `POST /pdoa/access-points/{id}/status` | PDOA | Activate/suspend APs |
| `GET/POST /pdoa/plans`, `PUT /pdoa/plans/{id}` | PDOA | Manage plans |
| `GET /pdoa/sessions/export.csv?date_from=&date_to=` | PDOA | Compliance log: mobile, device MAC, IP, AP, times, bytes (IST) |
| `GET /pdoa/registry/export` | PDOA | AP/PDO inventory for the C-DOT Central Registry (**field format still to be aligned with the WANI spec**) |

Full schemas: `http://<server>/docs` or `openapi.json` in this folder.

## How plans become hotspot limits

On `/wifi/login` (app) or portal login, the backend writes for the user's mobile number:
- `radcheck` Cleartext-Password = one-time random password
- `radreply` Session-Timeout (time left, max 4 h per session), Acct-Interim-Interval (300 s),
  ChilliSpot-Max-Total-Octets (data left), WISPr-Bandwidth-Max-Down/Up (plan speed)

CoovaChilli enforces them. Usage is read back from `radacct`; when a plan's data is used up it is marked
`exhausted` and the next queued plan starts immediately.

## Flutter integration

`flutter/pmwani_api.dart` is a ready client (packages `http`, `shared_preferences`). Suggested approach:
put a `Repository` interface between `PmwaniViewModel` and the data, keep the current simulation as
`MockRepository`, and add `ApiRepository` using this client. Switch feature by feature:
login → hotspots/plans → wallet/payments → Wi-Fi login → sessions → PDO → PDOA.

### Real Wi-Fi login on Android (the tricky part)
1. User joins the PM-WANI SSID. Before login there is no internet, so Android routes app traffic over
   mobile data. **Bind the app to the Wi-Fi network** (`ConnectivityManager.bindProcessToNetwork(wifiNetwork)`
   via a small Kotlin platform channel) for the next two steps.
2. Request `http://connectivitycheck.gstatic.com/generate_204` **without following redirects**. A 302 whose
   `Location` contains `/chilli?res=notyet` means "hotspot, not logged in".
3. Call `api.wifiLogin(Uri.parse(location))`. This call goes to the backend — allowed before login because the
   server IP is in CoovaChilli's `uamallowed`.
4. GET the returned `logon_url` (still bound to Wi-Fi). CoovaChilli replies with a redirect to
   `/chilli?res=success` → logged in. Then unbind (`bindProcessToNetwork(null)`).
5. A 402 `no_active_plan` means: show plans (free plan via `buyPlan(id, method: 'free')`).

Cleartext HTTP: until the domain + HTTPS is ready, allow cleartext for the server IP in a debug
`network_security_config.xml`. Do not ship production with cleartext enabled.

## Not done yet / next
- HTTPS + domain (needed for Razorpay webhooks and production app)
- Real SMS (MSG91 + DLT template) — code ready, needs credentials
- C-DOT Central Registry API — export endpoint gathers the data; format/API calls to be built from the WANI spec
- Real KYC verification provider (currently records submission; PDOA verifies manually)
- PDO revenue share / payouts
- Old MariaDB data from the POC is not migrated (test data only); MariaDB can be removed later
