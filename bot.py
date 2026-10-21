"""
╔══════════════════════════════════════════════════════════════════╗
║  👑 LUX STUDIO VIP v8 — REAL & FOCUSED                           ║
║  ─────────────────────────────────────────────────────────────── ║
║  FIXES:                                                          ║
║  • /ping never creates users (bot detection)                     ║
║  • No auto-key on page load — only on user action                ║
║  • Each user gets LUX-XXXX-XXXX token for charging               ║
║  • Profile page with token + invite                              ║
║  • Owner panel: search by token, charge credits                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, sys, io, time, secrets, hashlib, logging, asyncio, contextlib
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict, Counter

import httpx
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw, ImageFont
from fastapi import FastAPI, File, UploadFile, Header, HTTPException, Request, Form, Cookie
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
DSS_BASE_URL = "https://kali-imvx.onrender.com"
DSS_API_KEY  = "dss_SSiPIYB4EpWwVmfygQ1LTF2KymOjd-qxAQ4U3Q"
OWNER_PASSWORD = "xeon"
OWNER_SESSION_HOURS = 24
FREE_CREDITS = 30
MAX_FILE_MB = 15
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
PORT = int(os.getenv("PORT", "8000"))
RATE_WINDOW = 60
RATE_LIMIT = 40

DEFAULT_TASKS = {
    "daily_checkin": {"reward": 3, "label": "زيارة يومية"},
    "share_app": {"reward": 5, "label": "مشاركة التطبيق"},
    "invite_friend": {"reward": 15, "label": "دعوة صديق"},
}
DEFAULT_LINKS = [
    {"id": "t1", "title": "قناة تيليجرام", "url": "https://t.me/telegram", "reward": 10, "icon": "✈️"},
]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout, force=True,
)
log = logging.getLogger("lux")

# ═══════════════════════════════════════════════════════════════════
#  🛡️ BOT DETECTION — الحل الجذري لمشكلة ping
# ═══════════════════════════════════════════════════════════════════
BROWSER_SIGS = ["mozilla", "chrome", "safari", "firefox", "edge", "opera"]
BOT_SIGS = ["bot", "crawler", "spider", "uptime", "pingdom", "monitor", "cron",
            "curl", "wget", "python-requests", "httpx", "okhttp", "go-http",
            "render", "healthcheck", "health-check", "prerender", "headless",
            "phantom", "selenium", "puppeteer", "playwright", "googlebot",
            "bingbot", "yandex", "baidu", "duckduckbot", "ahrefs", "semrush"]


def is_human_request(request: Request) -> tuple:
    """
    Returns (is_human, reason).
    Ensures /api/keys/new only creates users for real browsers.
    """
    ua = (request.headers.get("user-agent", "") or "").lower().strip()
    accept = (request.headers.get("accept", "") or "").lower()
    lang = (request.headers.get("accept-language", "") or "").lower()

    if not ua or len(ua) < 10:
        return False, f"empty UA ({ua!r})"

    # Must have browser signature
    if not any(sig in ua for sig in BROWSER_SIGS):
        return False, f"not a browser ({ua[:50]})"

    # Reject bot keywords
    for sig in BOT_SIGS:
        if sig in ua:
            return False, f"bot signature '{sig}'"

    # Browsers always accept HTML
    if "text/html" not in accept and not accept.startswith("*/*"):
        return False, f"Accept header: {accept[:40]}"

    return True, "ok"


# ═══════════════════════════════════════════════════════════════════
#  REMBG LAZY LOADER
# ═══════════════════════════════════════════════════════════════════
REMBG_MODEL = "u2netp"
_rembg_remove = None
_rembg_ready = False
_rembg_lock = asyncio.Lock()
_rembg_event = asyncio.Event()


def _load_rembg_sync():
    global _rembg_remove, _rembg_ready
    try:
        log.info("📦 Loading rembg...")
        from rembg import remove as rr, new_session
        _rembg_remove = rr
        try:
            new_session(REMBG_MODEL)
            log.info(f"✅ '{REMBG_MODEL}' ready")
        except Exception as e: log.warning(f"⚠️ {e}")
    except Exception as e: log.error(f"❌ rembg: {e}")
    finally:
        _rembg_ready = True
        try: _rembg_event.set()
        except: pass


async def _ensure_rembg_loaded():
    if _rembg_ready: return
    async with _rembg_lock:
        if _rembg_ready: return
        await asyncio.to_thread(_load_rembg_sync)

# ═══════════════════════════════════════════════════════════════════
#  DSS CLIENT
# ═══════════════════════════════════════════════════════════════════
class DSSError(Exception): pass


class DSS:
    def __init__(self):
        self.base = DSS_BASE_URL.rstrip("/")
        self.headers = {"X-API-Key": DSS_API_KEY, "Content-Type": "application/json", "Accept": "application/json"}

    async def _req(self, method, path, retries=2, **kw):
        url = f"{self.base}{path}"; last = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    r = await c.request(method, url, headers=self.headers, **kw)
                    r.raise_for_status()
                    return r.json() if r.content else {}
            except httpx.HTTPStatusError as e:
                body = e.response.text[:200] if e.response is not None else ""
                last = DSSError(f"{method} {path} → {e.response.status_code}: {body}")
                if 400 <= e.response.status_code < 500 and e.response.status_code != 404: raise last
            except httpx.RequestError as e: last = DSSError(f"net {method} {path}: {e}")
            if attempt < retries: await asyncio.sleep(0.6)
        raise last or DSSError("unknown")

    async def list(self, col):
        try: res = await self._req("GET", f"/api/db/{col}")
        except DSSError as e:
            if "404" in str(e): return []
            raise
        if isinstance(res, list): return res
        if isinstance(res, dict):
            for k in ("items","data","results","records","posts","rows","documents","docs","list"):
                if isinstance(res.get(k), list): return res[k]
            if "id" in res or "_id" in res: return [res]
        return []

    async def create(self, col, data):
        res = await self._req("POST", f"/api/db/{col}", json=data)
        if isinstance(res, dict):
            for key in ("id","_id","uuid","key","ID"):
                if key in res: return {**data, **res}
            if isinstance(res.get("data"), dict): return {**data, **res["data"]}
        return {**data, "_raw": res}

    async def update(self, col, doc_id, data):
        for method in ("PATCH", "PUT"):
            try: return await self._req(method, f"/api/db/{col}/{doc_id}", json=data)
            except DSSError: continue
        try: return await self._req("POST", f"/api/db/{col}", json={**data, "id": doc_id})
        except DSSError as e: log.warning(f"update fallback: {e}"); return {}

    async def delete(self, col, doc_id):
        try: await self._req("DELETE", f"/api/db/{col}/{doc_id}"); return True
        except DSSError: return False

    @staticmethod
    def doc_id(doc):
        if not isinstance(doc, dict): return None
        for k in ("id","_id","uuid","key","ID"):
            if k in doc and doc[k] is not None: return doc[k]
        return None


dss = DSS()

# ═══════════════════════════════════════════════════════════════════
#  RATE + OWNER SESSIONS
# ═══════════════════════════════════════════════════════════════════
_rate_buckets: dict = defaultdict(list)
_owner_sessions: dict = {}


def rate_check(key: str) -> bool:
    now = time.time()
    _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < RATE_WINDOW]
    if len(_rate_buckets[key]) >= RATE_LIMIT: return False
    _rate_buckets[key].append(now); return True


def new_owner_session() -> str:
    token = secrets.token_urlsafe(32)
    _owner_sessions[token] = time.time() + OWNER_SESSION_HOURS * 3600
    now = time.time()
    for t in list(_owner_sessions.keys()):
        if _owner_sessions[t] < now: _owner_sessions.pop(t, None)
    return token


def check_owner(token):
    if not token: return False
    exp = _owner_sessions.get(token)
    if not exp: return False
    if exp < time.time(): _owner_sessions.pop(token, None); return False
    return True


def require_owner(lux_owner: Optional[str] = Cookie(None)):
    if not check_owner(lux_owner): raise HTTPException(401, "Owner session required")
    return True

# ═══════════════════════════════════════════════════════════════════
#  USERS + TOKENS
# ═══════════════════════════════════════════════════════════════════
def sha256(s: str) -> str: return hashlib.sha256(s.encode()).hexdigest()


def gen_user_token() -> str:
    """LUX-XXXX-XXXX — human-readable user token for charging."""
    hex_str = secrets.token_hex(4).upper()
    return f"LUX-{hex_str[:4]}-{hex_str[4:8]}"


async def get_user_by_id(uid) -> Optional[dict]:
    for u in await dss.list("users"):
        if str(u.get("id")) == str(uid) or str(u.get("_id")) == str(uid): return u
    return None


async def get_user_by_token(token: str) -> Optional[dict]:
    token = token.strip().upper()
    for u in await dss.list("users"):
        if str(u.get("user_token", "")).upper() == token: return u
    return None


async def find_api_key(plain: str) -> Optional[dict]:
    target = sha256(plain.strip())
    try: keys = await dss.list("api_keys")
    except DSSError as e: log.error(f"find_api_key: {e}"); return None
    for k in keys:
        if k.get("hash") == target and k.get("active", True): return k
    return None


async def ip_key_count(ip: str) -> int:
    cutoff = time.time() - 86400
    count = 0
    try:
        for u in await dss.list("users"):
            if u.get("ip") != ip: continue
            try:
                ts = datetime.fromisoformat(str(u.get("created_at","")).replace("Z","+00:00")).timestamp()
                if ts >= cutoff: count += 1
            except: pass
    except: pass
    return count


async def create_free_key(ip: str, invite_code: str = "", ua: str = "") -> dict:
    user_id = secrets.token_urlsafe(12)
    user_token = gen_user_token()
    # Ensure uniqueness
    for _ in range(5):
        if not await get_user_by_token(user_token): break
        user_token = gen_user_token()

    user_doc = {
        "id": user_id, "user_token": user_token, "ip": ip,
        "credits": FREE_CREDITS, "usage_count": 0, "is_banned": False,
        "invite_code": secrets.token_urlsafe(8),
        "invited_by": invite_code or "",
        "user_agent": ua[:200],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await dss.create("users", user_doc)
    plain = f"lux_{secrets.token_urlsafe(32)}"
    prefix = plain[:12]
    await dss.create("api_keys", {
        "id": secrets.token_urlsafe(12), "user_id": user_id,
        "hash": sha256(plain), "prefix": prefix, "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # Invite reward
    if invite_code:
        try:
            for u in await dss.list("users"):
                if u.get("invite_code") == invite_code and str(u.get("id")) != user_id:
                    did = DSS.doc_id(u)
                    if did is not None:
                        await dss.update("users", did, {
                            "credits": int(u.get("credits", 0)) + DEFAULT_TASKS["invite_friend"]["reward"],
                        })
                    break
        except: pass
    log.info(f"✅ New user {user_token} (UA: {ua[:40]})")
    return {"api_key": plain, "prefix": prefix, "credits": FREE_CREDITS,
            "user_id": user_id, "user_token": user_token, "invite_code": user_doc["invite_code"]}


async def deduct_credit(user_id) -> bool:
    user = await get_user_by_id(user_id)
    if not user: return False
    credits = int(user.get("credits", 0))
    if credits < 1: return False
    did = DSS.doc_id(user)
    if did is None: return False
    try:
        await dss.update("users", did, {
            "credits": credits - 1,
            "usage_count": int(user.get("usage_count", 0)) + 1,
        }); return True
    except: return False


async def add_credits(user_id, amount) -> bool:
    user = await get_user_by_id(user_id)
    if not user: return False
    did = DSS.doc_id(user)
    if did is None: return False
    try:
        await dss.update("users", did, {"credits": int(user.get("credits", 0)) + int(amount)})
        return True
    except: return False


async def log_usage(user_id, action, status, duration_ms, size_bytes=0):
    try:
        await dss.create("usage_logs", {
            "id": secrets.token_urlsafe(12), "user_id": user_id,
            "action": action, "status": status,
            "duration_ms": duration_ms, "size_bytes": size_bytes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except: pass


async def authenticate(x_api_key) -> dict:
    if not x_api_key: raise HTTPException(401, "X-API-Key required")
    rec = await find_api_key(x_api_key.strip())
    if not rec: raise HTTPException(401, "Invalid or revoked key")
    user = await get_user_by_id(rec.get("user_id"))
    if user and user.get("is_banned"): raise HTTPException(403, "Account banned")
    return rec

# ═══════════════════════════════════════════════════════════════════
#  TASKS CONFIG
# ═══════════════════════════════════════════════════════════════════
async def get_tasks_config() -> dict:
    try:
        for doc in await dss.list("app_config"):
            if doc.get("id") == "tasks" or doc.get("_id") == "tasks":
                return {
                    "daily_checkin": doc.get("daily_checkin", DEFAULT_TASKS["daily_checkin"]),
                    "share_app": doc.get("share_app", DEFAULT_TASKS["share_app"]),
                    "invite_friend": doc.get("invite_friend", DEFAULT_TASKS["invite_friend"]),
                    "links": doc.get("links", DEFAULT_LINKS),
                }
    except: pass
    return {**DEFAULT_TASKS, "links": DEFAULT_LINKS}


async def save_tasks_config(cfg: dict) -> bool:
    try:
        existing = None
        for doc in await dss.list("app_config"):
            if doc.get("id") == "tasks" or doc.get("_id") == "tasks":
                existing = doc; break
        if existing:
            did = DSS.doc_id(existing)
            if did is not None: await dss.update("app_config", did, cfg)
        else:
            await dss.create("app_config", {"id": "tasks", **cfg})
        return True
    except Exception as e:
        log.error(f"save_tasks_config: {e}"); return False


async def get_user_claims(user_id) -> list:
    try: return [c for c in await dss.list("user_tasks") if c.get("user_id") == user_id]
    except: return []


async def log_claim(user_id, task_id):
    await dss.create("user_tasks", {
        "id": secrets.token_urlsafe(12), "user_id": user_id, "task_id": task_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    })

# ═══════════════════════════════════════════════════════════════════
#  IMAGE TOOLS (21 tools compact)
# ═══════════════════════════════════════════════════════════════════
def _open(data):
    try:
        img = Image.open(io.BytesIO(data)); img.load(); return img
    except Exception as e: raise ValueError(f"Invalid: {e}")


def _save(img, fmt="PNG", **kw):
    out = io.BytesIO(); img.save(out, format=fmt, **kw); return out.getvalue()


def t_remove_bg(d, **kw):
    if _rembg_remove is None: raise RuntimeError("Model loading")
    try: return _rembg_remove(d, model_name=REMBG_MODEL), "image/png", "png"
    except: return _rembg_remove(d), "image/png", "png"


def t_enhance(d, **kw):
    img = _open(d)
    a = img.mode in ("RGBA","LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if a else "RGB")
    w, h = img.size; nw, nh = w * 2, h * 2
    if max(nw, nh) > 2048: r = 2048 / max(nw, nh); nw, nh = int(nw*r), int(nh*r)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    img = ImageEnhance.Sharpness(img).enhance(1.4)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.18)
    return (_save(img, "PNG", optimize=True), "image/png", "png") if a else (_save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


def t_compress(d, quality=75, **kw):
    img = _open(d)
    if img.mode not in ("RGB","RGBA"): img = img.convert("RGBA" if img.mode in ("P","LA") else "RGB")
    q = max(10, min(95, int(quality)))
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=q, optimize=True), "image/jpeg", "jpg")


def t_convert(d, target="png", **kw):
    img = _open(d); target = target.lower()
    if target in ("jpg","jpeg"):
        if img.mode != "RGB": img = img.convert("RGB")
        return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"
    elif target == "png":
        if img.mode not in ("RGBA","RGB"): img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        return _save(img, "PNG", optimize=True), "image/png", "png"
    else:
        if img.mode not in ("RGB","RGBA"): img = img.convert("RGB")
        return _save(img, "WEBP", quality=90, method=6), "image/webp", "webp"


def t_grayscale(d, **kw):
    img = _open(d)
    if img.mode in ("RGBA","LA"):
        a = img.split()[-1]; g = ImageOps.grayscale(img.convert("RGB")).convert("RGBA"); g.putalpha(a)
        return _save(g, "PNG", optimize=True), "image/png", "png"
    return _save(ImageOps.grayscale(img.convert("RGB")), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_invert(d, **kw):
    img = _open(d)
    if img.mode == "RGBA":
        r, g, b, a = img.split(); inv = ImageOps.invert(Image.merge("RGB", (r,g,b)))
        return _save(Image.merge("RGBA", (*inv.split(), a)), "PNG", optimize=True), "image/png", "png"
    return _save(ImageOps.invert(img.convert("RGB")), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_rotate(d, angle=90, **kw):
    img = _open(d); angle = int(angle) % 360
    img = img.rotate(-angle, expand=True, fillcolor=(0,0,0,0) if img.mode == "RGBA" else None)
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


def t_flip(d, direction="h", **kw):
    img = _open(d)
    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT if direction == "h" else Image.Transpose.FLIP_TOP_BOTTOM)
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


def t_blur(d, radius=5, **kw):
    img = _open(d); r = max(1, min(30, int(radius)))
    img = img.filter(ImageFilter.GaussianBlur(radius=r))
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


def t_sharpen(d, **kw):
    img = _open(d)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=2))
    img = ImageEnhance.Sharpness(img).enhance(1.8)
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=95, optimize=True), "image/jpeg", "jpg")


def _filter_color(d, rm=1, gm=1, bm=1, sat=1):
    img = _open(d).convert("RGB")
    if sat != 1: img = ImageEnhance.Color(img).enhance(sat)
    r, g, b = img.split()
    r = r.point(lambda i: min(255, int(i * rm))); g = g.point(lambda i: min(255, int(i * gm))); b = b.point(lambda i: min(255, int(i * bm)))
    return _save(Image.merge("RGB", (r, g, b)), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_warm(d, **kw): return _filter_color(d, 1.15, 1.02, 0.88)
def t_cool(d, **kw): return _filter_color(d, 0.88, 1.02, 1.15)


def t_vintage(d, **kw):
    img = _open(d).convert("RGB")
    img = ImageEnhance.Color(img).enhance(0.6); img = ImageEnhance.Contrast(img).enhance(1.1)
    r, g, b = img.split()
    r = r.point(lambda i: min(255, i + 15)); b = b.point(lambda i: max(0, i - 15))
    return _save(Image.merge("RGB", (r, g, b)), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_sepia(d, **kw):
    img = _open(d).convert("RGB"); px = img.load(); w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            px[x, y] = (min(255, int(0.393*r+0.769*g+0.189*b)),
                        min(255, int(0.349*r+0.686*g+0.168*b)),
                        min(255, int(0.272*r+0.534*g+0.131*b)))
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_adjust(d, brightness=0, contrast=0, saturation=0, **kw):
    img = _open(d); a = img.mode in ("RGBA","LA")
    alpha = img.split()[-1] if a else None
    img = img.convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(max(0.1, 1 + int(brightness)/100))
    img = ImageEnhance.Contrast(img).enhance(max(0.1, 1 + int(contrast)/100))
    img = ImageEnhance.Color(img).enhance(max(0.0, 1 + int(saturation)/100))
    if a and alpha:
        img = Image.merge("RGBA", (*img.split(), alpha))
        return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_resize(d, width=0, height=0, **kw):
    img = _open(d); w, h = img.size
    width, height = int(width), int(height)
    if width <= 0 and height <= 0: raise ValueError("Specify width/height")
    if width > 0 and height > 0: nw, nh = width, height
    elif width > 0: nw, nh = width, int(h*width/w)
    else: nh, nw = height, int(w*height/h)
    nw, nh = min(nw, 4096), min(nh, 4096)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


def t_crop(d, ratio="1:1", **kw):
    img = _open(d); w, h = img.size
    try: rw, rh = map(int, ratio.split(":"))
    except: rw, rh = 1, 1
    tr = rw/rh; cur = w/h
    if cur > tr: nh = h; nw = int(h*tr); x, y = (w-nw)//2, 0
    else: nw = w; nh = int(w/tr); x, y = 0, (h-nh)//2
    img = img.crop((x, y, x+nw, y+nh))
    return (_save(img, "PNG", optimize=True), "image/png", "png") if img.mode == "RGBA" else (_save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


def t_watermark(d, text="Lux Studio", position="br", opacity=60, **kw):
    img = _open(d).convert("RGBA")
    layer = Image.new("RGBA", img.size, (0,0,0,0))
    draw = ImageDraw.Draw(layer)
    try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(20, img.width//20))
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0,0), text, font=font); tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    m = 20
    pos = {"tl":(m,m), "tr":(img.width-tw-m,m), "bl":(m,img.height-th-m),
           "br":(img.width-tw-m,img.height-th-m), "c":((img.width-tw)//2,(img.height-th)//2)}.get(position, (img.width-tw-m, img.height-th-m))
    draw.text(pos, text, font=font, fill=(255,255,255,int(255*int(opacity)/100)))
    return _save(Image.alpha_composite(img, layer), "PNG", optimize=True), "image/png", "png"


def t_bg_color(d, color="#ffffff", **kw):
    img = _open(d).convert("RGBA")
    hx = color.lstrip("#")
    try: r, g, b = int(hx[0:2],16), int(hx[2:4],16), int(hx[4:6],16)
    except: r, g, b = 255, 255, 255
    bg = Image.new("RGBA", img.size, (r,g,b,255))
    return _save(Image.alpha_composite(bg, img).convert("RGB"), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_round_corners(d, radius=40, **kw):
    img = _open(d).convert("RGBA"); w, h = img.size
    radius = min(int(radius), min(w,h)//2)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,w,h), radius=radius, fill=255)
    img.putalpha(mask)
    return _save(img, "PNG", optimize=True), "image/png", "png"


def t_border(d, thickness=20, color="#00e0ff", **kw):
    img = _open(d); hx = color.lstrip("#")
    try: rgb = (int(hx[0:2],16), int(hx[2:4],16), int(hx[4:6],16))
    except: rgb = (0,224,255)
    fill = (*rgb, 255) if img.mode == "RGBA" else rgb
    out = ImageOps.expand(img, border=int(thickness), fill=fill)
    return (_save(out, "PNG", optimize=True), "image/png", "png") if out.mode == "RGBA" else (_save(out, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg")


TOOLS = {
    "remove-bg": t_remove_bg, "enhance": t_enhance, "compress": t_compress, "convert": t_convert,
    "grayscale": t_grayscale, "invert": t_invert, "rotate": t_rotate, "flip": t_flip,
    "blur": t_blur, "sharpen": t_sharpen, "warm": t_warm, "cool": t_cool,
    "vintage": t_vintage, "sepia": t_sepia, "adjust": t_adjust, "resize": t_resize,
    "crop": t_crop, "watermark": t_watermark, "bg-color": t_bg_color,
    "round-corners": t_round_corners, "border": t_border,
}

# ═══════════════════════════════════════════════════════════════════
#  LIFESPAN
# ═══════════════════════════════════════════════════════════════════
async def _keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url: return
    while True:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                await c.get(f"{url}/ping")   # ← هذا نظيف، لا ينشئ مستخدمين
        except: pass


@contextlib.asynccontextmanager
async def lifespan(app):
    bg = asyncio.create_task(_ensure_rembg_loaded())
    ka = asyncio.create_task(_keep_alive())
    log.info("🚀 Lux Studio VIP v8 ready")
    try: yield
    finally:
        for t in (bg, ka): t.cancel()
        for t in (bg, ka):
            with contextlib.suppress(asyncio.CancelledError, Exception): await t

# ═══════════════════════════════════════════════════════════════════
#  FASTAPI
# ═══════════════════════════════════════════════════════════════════
app = FastAPI(title="Lux Studio VIP", version="8.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
async def index(): return HTML_PAGE


@app.get("/ping")
async def ping(request: Request):
    """Ping — pure, no side effects. Used by Render health check + external cron."""
    return {"status": "alive", "ts": time.time(), "rembg_ready": _rembg_ready}


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Lux Studio VIP", "short_name": "Lux VIP", "start_url": "/",
        "display": "standalone", "orientation": "portrait-primary",
        "background_color": "#05070f", "theme_color": "#00e0ff",
        "description": "21 أداة مصمم + مهام + جوائز",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
    })


@app.get("/icon.svg")
async def icon(): return Response(content=SVG_ICON, media_type="image/svg+xml")


@app.get("/sw.js")
async def sw(): return Response(content=SW_JS, media_type="application/javascript")


@app.get("/api/health")
async def health():
    r = {"status": "ok", "rembg_ready": _rembg_ready, "version": "8.0.0"}
    try:
        users = await dss.list("users"); r["dss"] = "connected"; r["dss_users"] = len(users)
    except Exception as e:
        r["dss"] = f"error: {e}"; r["status"] = "degraded"
    return r


# ═══════════════ Keys ═══════════════
@app.post("/api/keys/new")
async def api_new_key(request: Request, invite: str = ""):
    """Creates a new user — ONLY for real browsers."""
    human, reason = is_human_request(request)
    if not human:
        log.warning(f"🚫 Blocked non-human: {reason}")
        raise HTTPException(403, f"Automated requests not allowed: {reason}")

    ip = request.client.host if request.client else "unknown"
    if not rate_check(f"key:{ip}"): raise HTTPException(429, "Too many attempts")
    try:
        if await ip_key_count(ip) >= 3: raise HTTPException(429, "Max 3 keys/day per IP")
        ua = request.headers.get("user-agent", "")[:200]
        return {**await create_free_key(ip, invite, ua), "message": "Save this key"}
    except HTTPException: raise
    except DSSError as e: raise HTTPException(503, f"DB: {e}")


@app.get("/api/me")
async def api_me(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec.get("user_id"))
    if not user: raise HTTPException(404, "Not found")
    try: logs = [l for l in await dss.list("usage_logs") if l.get("user_id") == rec.get("user_id")]
    except: logs = []
    return {
        "prefix": rec.get("prefix"),
        "credits": int(user.get("credits", 0)),
        "total_operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
        "user_token": user.get("user_token", ""),
        "invite_code": user.get("invite_code", ""),
        "created_at": user.get("created_at"),
    }


@app.get("/api/history")
async def api_history(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    try:
        logs = [l for l in await dss.list("usage_logs") if l.get("user_id") == rec.get("user_id")]
        logs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {"items": logs[:50]}
    except: return {"items": []}


# ═══════════════ Tasks ═══════════════
@app.get("/api/tasks")
async def api_tasks(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    uid = rec["user_id"]
    cfg = await get_tasks_config()
    claims = await get_user_claims(uid)
    claimed_ids = [c.get("task_id") for c in claims]
    now = datetime.now(timezone.utc)
    daily_claimed = False
    for c in claims:
        if c.get("task_id") == "daily_checkin":
            try:
                ts = datetime.fromisoformat(str(c.get("claimed_at","")).replace("Z","+00:00"))
                if (now - ts).total_seconds() < 86400: daily_claimed = True
            except: pass
    return {
        "daily_checkin": {**cfg["daily_checkin"], "claimed": daily_claimed},
        "share_app": {**cfg["share_app"], "claimed": "share_app" in claimed_ids},
        "invite_friend": {**cfg["invite_friend"], "claimed": "invite_friend" in claimed_ids},
        "links": [{**lk, "claimed": f"link:{lk['id']}" in claimed_ids} for lk in cfg["links"]],
    }


@app.post("/api/tasks/checkin")
async def api_checkin(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    uid = rec["user_id"]
    claims = await get_user_claims(uid)
    now = datetime.now(timezone.utc)
    for c in claims:
        if c.get("task_id") == "daily_checkin":
            try:
                ts = datetime.fromisoformat(str(c.get("claimed_at","")).replace("Z","+00:00"))
                if (now - ts).total_seconds() < 86400: raise HTTPException(400, "Already claimed today")
            except HTTPException: raise
            except: pass
    cfg = await get_tasks_config()
    reward = cfg["daily_checkin"]["reward"]
    await add_credits(uid, reward)
    await log_claim(uid, "daily_checkin")
    return {"ok": True, "reward": reward, "message": f"+{reward} نقطة!"}


@app.post("/api/tasks/share")
async def api_share(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    uid = rec["user_id"]
    claims = await get_user_claims(uid)
    if "share_app" in [c.get("task_id") for c in claims]:
        raise HTTPException(400, "Already claimed")
    cfg = await get_tasks_config()
    reward = cfg["share_app"]["reward"]
    await add_credits(uid, reward)
    await log_claim(uid, "share_app")
    return {"ok": True, "reward": reward, "message": f"+{reward} نقطة!"}


@app.post("/api/tasks/claim-link")
async def api_claim_link(link_id: str = Form(...), x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    uid = rec["user_id"]
    cfg = await get_tasks_config()
    link = next((lk for lk in cfg["links"] if lk["id"] == link_id), None)
    if not link: raise HTTPException(404, "Link not found")
    claims = await get_user_claims(uid)
    if f"link:{link_id}" in [c.get("task_id") for c in claims]:
        raise HTTPException(400, "Already claimed")
    reward = link.get("reward", 10)
    await add_credits(uid, reward)
    await log_claim(uid, f"link:{link_id}")
    return {"ok": True, "reward": reward, "message": f"+{reward} نقطة!"}


# ═══════════════ Image processing ═══════════════
@app.post("/api/tool/{tool_name}")
async def api_tool(tool_name: str, request: Request, file: UploadFile = File(...),
                  x_api_key: Optional[str] = Header(None)):
    if tool_name not in TOOLS: raise HTTPException(404, f"Unknown tool: {tool_name}")
    rec = await authenticate(x_api_key)
    uid = rec["user_id"]
    user = await get_user_by_id(uid)
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "Out of credits")
    if not rate_check(f"user:{uid}"): raise HTTPException(429, "Slow down")
    data = await file.read()
    if not data: raise HTTPException(400, "Empty file")
    if len(data) > MAX_FILE_BYTES: raise HTTPException(413, f"Max {MAX_FILE_MB}MB")
    if tool_name == "remove-bg" and not _rembg_ready:
        try: await asyncio.wait_for(_rembg_event.wait(), timeout=120.0)
        except asyncio.TimeoutError: raise HTTPException(503, "Model loading")
    params = dict(request.query_params)
    for k in ("quality","radius","angle","width","height","brightness","contrast","saturation","opacity","thickness"):
        if k in params:
            try: params[k] = int(params[k])
            except: params.pop(k, None)
    t0 = time.time()
    try: result = await asyncio.to_thread(TOOLS[tool_name], data, **params)
    except ValueError as e:
        await log_usage(uid, tool_name, "error", 0); raise HTTPException(400, str(e))
    except Exception as e:
        await log_usage(uid, tool_name, "error", 0); raise HTTPException(500, str(e))
    if isinstance(result, tuple):
        result, media_type, ext = result if len(result) == 3 else (result[0], f"image/{result[1]}", result[1])
    else: media_type, ext = "image/png", "png"
    await deduct_credit(uid)
    await log_usage(uid, tool_name, "success", int((time.time()-t0)*1000), len(result))
    return Response(content=result, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="lux_{tool_name}.{ext}"'})


# ═══════════════ Owner ═══════════════
@app.get("/owner", response_class=HTMLResponse)
async def owner_page(lux_owner: Optional[str] = Cookie(None)):
    if check_owner(lux_owner): return OWNER_DASHBOARD_HTML
    return OWNER_LOGIN_HTML


@app.post("/owner/login")
async def owner_login(password: str = Form(...)):
    if password != OWNER_PASSWORD: return RedirectResponse("/owner?error=1", status_code=303)
    token = new_owner_session()
    resp = RedirectResponse("/owner", status_code=303)
    resp.set_cookie("lux_owner", token, max_age=OWNER_SESSION_HOURS*3600, httponly=True, samesite="lax")
    return resp


@app.post("/owner/logout")
async def owner_logout(lux_owner: Optional[str] = Cookie(None)):
    if lux_owner: _owner_sessions.pop(lux_owner, None)
    resp = RedirectResponse("/owner", status_code=303); resp.delete_cookie("lux_owner"); return resp


@app.get("/api/owner/stats")
async def owner_stats(lux_owner: Optional[str] = Cookie(None)):
    require_owner(lux_owner)
    try: users = await dss.list("users"); keys = await dss.list("api_keys"); logs = await dss.list("usage_logs")
    except DSSError as e: raise HTTPException(503, str(e))
    return {
        "users": len(users), "keys_total": len(keys),
        "keys_active": sum(1 for k in keys if k.get("active")),
        "operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
        "banned": sum(1 for u in users if u.get("is_banned")),
        "total_credits": sum(int(u.get("credits", 0)) for u in users),
    }


@app.get("/api/owner/users")
async def owner_users(lux_owner: Optional[str] = Cookie(None)):
    require_owner(lux_owner)
    try: users = await dss.list("users"); keys = await dss.list("api_keys")
    except DSSError as e: raise HTTPException(503, str(e))
    key_by_user = {}
    for k in keys:
        uid = k.get("user_id")
        if uid and (uid not in key_by_user or not key_by_user[uid].get("active")): key_by_user[uid] = k
    items = []
    for u in users:
        uid = str(u.get("id") or u.get("_id") or "")
        k = key_by_user.get(uid) or {}
        items.append({
            "id": uid, "user_token": u.get("user_token", "-"),
            "ip": u.get("ip","-"), "credits": int(u.get("credits",0)),
            "usage_count": int(u.get("usage_count",0)), "is_banned": bool(u.get("is_banned")),
            "created_at": u.get("created_at",""), "prefix": k.get("prefix","-"),
            "ua": (u.get("user_agent","") or "")[:60],
        })
    items.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return {"items": items[:300], "total": len(items)}


@app.post("/api/owner/charge-by-token")
async def owner_charge_by_token(user_token: str = Form(...), amount: int = Form(...),
                                lux_owner: Optional[str] = Cookie(None)):
    """Charge credits by user token (LUX-XXXX-XXXX)."""
    require_owner(lux_owner)
    if amount == 0: raise HTTPException(400, "Amount is zero")
    user = await get_user_by_token(user_token)
    if not user:
        # Try by ID
        user = await get_user_by_id(user_token)
    if not user: raise HTTPException(404, "User not found")
    uid = str(user.get("id"))
    if not await add_credits(uid, amount): raise HTTPException(500, "Failed")
    return {"ok": True, "user_id": uid, "user_token": user.get("user_token"),
            "new_credits": int(user.get("credits", 0)) + amount}


@app.post("/api/owner/user/credits")
async def owner_user_credits(user_id: str = Form(...), amount: int = Form(...),
                            lux_owner: Optional[str] = Cookie(None)):
    require_owner(lux_owner)
    if amount == 0: raise HTTPException(400, "Amount is zero")
    if not await add_credits(user_id, amount): raise HTTPException(404, "Not found")
    return {"ok": True}


@app.post("/api/owner/user/ban")
async def owner_user_ban(user_id: str = Form(...), ban: bool = Form(True),
                        lux_owner: Optional[str] = Cookie(None)):
    require_owner(lux_owner)
    user = await get_user_by_id(user_id)
    if not user: raise HTTPException(404, "Not found")
    did = DSS.doc_id(user)
    if did is None: raise HTTPException(400, "Bad id")
    await dss.update("users", did, {"is_banned": ban})
    return {"ok": True}


@app.get("/api/owner/config")
async def owner_config(lux_owner: Optional[str] = Cookie(None)):
    require_owner(lux_owner)
    return await get_tasks_config()


@app.post("/api/owner/config")
async def owner_save_config(
    daily_reward: int = Form(...), share_reward: int = Form(...), invite_reward: int = Form(...),
    links_json: str = Form(...), lux_owner: Optional[str] = Cookie(None)
):
    require_owner(lux_owner)
    import json as _json
    try: links = _json.loads(links_json)
    except: raise HTTPException(400, "Invalid links JSON")
    cfg = {
        "daily_checkin": {"reward": daily_reward, "label": "زيارة يومية"},
        "share_app": {"reward": share_reward, "label": "مشاركة التطبيق"},
        "invite_friend": {"reward": invite_reward, "label": "دعوة صديق"},
        "links": links,
    }
    if not await save_tasks_config(cfg): raise HTTPException(500, "Save failed")
    return {"ok": True}


@app.post("/api/owner/change-password")
async def owner_change_password(old_password: str = Form(...), new_password: str = Form(...),
                                lux_owner: Optional[str] = Cookie(None)):
    require_owner(lux_owner)
    global OWNER_PASSWORD
    if old_password != OWNER_PASSWORD: raise HTTPException(401, "Wrong old password")
    if len(new_password) < 4: raise HTTPException(400, "Too short")
    OWNER_PASSWORD = new_password
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════
#  MAIN HTML
# ═══════════════════════════════════════════════════════════════════
HTML_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#00e0ff">
<title>Lux Studio VIP</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#05070f;--bg2:#0a0e1a;--surface:rgba(255,255,255,.035);--surface2:rgba(255,255,255,.08);--border:rgba(255,255,255,.08);--text:#e8ecf4;--muted:#8a94a8;--cyan:#00e0ff;--success:#00ffa3;--danger:#ff4d6d;--warn:#ffb020;--grad:linear-gradient(135deg,#00e0ff 0%,#7c5cff 50%,#ff2d95 100%);--drawer-w:280px;--hh:60px}
body.light{--bg:#f5f7fb;--bg2:#e8ecf4;--surface:#fff;--surface2:#f0f3f8;--border:rgba(0,0,0,.08);--text:#0a0e1a;--muted:#5a6478}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Tajawal',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(circle at 15% 20%,rgba(0,224,255,.10),transparent 35%),radial-gradient(circle at 85% 15%,rgba(124,92,255,.12),transparent 35%)}
body.light::before{opacity:.35}
a{color:var(--cyan);text-decoration:none}
header{position:fixed;top:0;left:0;right:0;height:var(--hh);z-index:100;backdrop-filter:blur(20px);background:rgba(5,7,15,.85);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 14px}
body.light header{background:rgba(245,247,251,.85)}
.ham{background:transparent;border:1px solid var(--border);color:var(--text);width:42px;height:42px;border-radius:10px;cursor:pointer;font-size:1.3rem}
.ham:hover{background:var(--surface2)}
.h-title{font-weight:900;font-size:1rem;text-align:center;flex:1}
.h-title small{display:block;font-size:.65rem;color:var(--muted);font-weight:600}
.icon-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);width:42px;height:42px;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1rem;text-decoration:none}
.icon-btn:hover{background:rgba(255,255,255,.12)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;opacity:0;pointer-events:none;transition:opacity .3s;backdrop-filter:blur(4px)}
.overlay.open{opacity:1;pointer-events:all}
.drawer{position:fixed;top:0;bottom:0;right:-320px;width:var(--drawer-w);background:var(--bg2);border-left:1px solid var(--border);z-index:201;transition:right .3s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;overflow-y:auto}
body.light .drawer{background:#fff}
.drawer.open{right:0}
.d-head{padding:20px;border-bottom:1px solid var(--border);text-align:center}
.d-logo{font-size:1.4rem;font-weight:900}
.d-logo span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.d-user{margin-top:10px;padding:10px;background:var(--surface);border-radius:10px;font-size:.78rem}
.d-user .cr{font-size:1.2rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:'JetBrains Mono',monospace}
.d-user .tk{font-size:.68rem;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:4px;direction:ltr}
.d-nav{flex:1;padding:10px 0}
.nav-item{display:flex;align-items:center;gap:12px;padding:13px 20px;color:var(--text);cursor:pointer;border:none;background:transparent;width:100%;text-align:right;font-family:inherit;font-size:.92rem;font-weight:600;transition:background .2s;border-right:3px solid transparent}
.nav-item:hover{background:var(--surface)}
.nav-item.active{background:var(--surface2);border-right-color:var(--cyan);color:var(--cyan)}
.nav-item .ico{font-size:1.3rem;width:26px;text-align:center}
.d-foot{padding:14px;border-top:1px solid var(--border);font-size:.72rem;color:var(--muted);text-align:center}
main{padding-top:calc(var(--hh) + 14px);padding-bottom:40px;max-width:1000px;margin:0 auto;padding-left:14px;padding-right:14px;min-height:100vh}
.page{animation:fadeIn .25s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.page h1{font-size:1.4rem;font-weight:900;margin-bottom:4px}
.page .sub{color:var(--muted);font-size:.85rem;margin-bottom:20px}
.hidden{display:none !important}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:18px;backdrop-filter:blur(10px);margin-bottom:14px}
.card h3{font-size:1.05rem;margin-bottom:10px;font-weight:700}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px;text-align:center}
.stat .num{font-size:1.6rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:'JetBrains Mono',monospace}
.stat .lbl{font-size:.72rem;color:var(--muted);margin-top:2px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:12px 20px;border-radius:12px;border:none;cursor:pointer;font-family:inherit;font-size:.9rem;font-weight:700;transition:all .2s;text-decoration:none}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--grad);color:#05070f;box-shadow:0 8px 30px rgba(124,92,255,.4)}
.btn-primary:hover:not(:disabled){transform:translateY(-2px)}
.btn-secondary{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-secondary:hover:not(:disabled){background:rgba(255,255,255,.12)}
.btn-block{width:100%}
.btn-lg{padding:15px 26px;font-size:.95rem}
.upload-zone{border:2px dashed var(--border);border-radius:16px;padding:38px 20px;text-align:center;cursor:pointer;transition:all .25s;background:var(--surface);margin-bottom:16px}
.upload-zone:hover{border-color:var(--cyan);background:rgba(0,224,255,.05)}
.upload-zone svg{width:42px;height:42px;stroke:var(--cyan);margin-bottom:10px}
.upload-zone h3{font-size:1rem;margin-bottom:4px}
.upload-zone p{color:var(--muted);font-size:.8rem}
.upload-zone input{display:none}
.tools-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(105px,1fr));gap:8px}
.tool-btn{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:14px 6px;border-radius:12px;cursor:pointer;font-family:inherit;font-weight:700;font-size:.78rem;transition:all .2s;display:flex;flex-direction:column;align-items:center;gap:5px;text-align:center}
.tool-btn:hover:not(:disabled){background:rgba(0,224,255,.1);border-color:var(--cyan);transform:translateY(-2px)}
.tool-btn .ic{font-size:1.5rem}
.preview-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
@media(max-width:600px){.preview-row{grid-template-columns:1fr}}
.img-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:8px;text-align:center}
.img-box .label{font-size:.68rem;color:var(--muted);text-transform:uppercase;font-weight:700;margin-bottom:6px}
.img-box img{max-width:100%;max-height:300px;border-radius:8px;display:block;margin:0 auto}
.checker{background-image:linear-gradient(45deg,#1a1f2e 25%,transparent 25%),linear-gradient(-45deg,#1a1f2e 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#1a1f2e 75%),linear-gradient(-45deg,transparent 75%,#1a1f2e 75%);background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0;background-color:#0d1220;border-radius:8px;padding:6px}
.loading{display:flex;flex-direction:column;align-items:center;padding:30px;gap:10px}
.spinner{width:40px;height:40px;border-radius:50%;border:4px solid var(--border);border-top-color:var(--cyan);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading p{color:var(--muted);font-size:.85rem}
.form-group{margin-bottom:14px}
.form-group label{display:block;margin-bottom:5px;font-weight:600;font-size:.82rem;color:var(--muted)}
.form-input{width:100%;padding:11px 13px;border-radius:10px;border:1px solid var(--border);background:var(--bg2);color:var(--text);font-family:inherit;font-size:.88rem}
.form-input:focus{outline:none;border-color:var(--cyan)}
.key-display{display:flex;gap:6px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;align-items:center;font-family:'JetBrains Mono',monospace;font-size:.75rem;margin-bottom:8px;word-break:break-all}
.key-display span{flex:1;color:var(--cyan);text-align:left;direction:ltr;overflow:hidden}
.key-display button{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px 9px;border-radius:6px;cursor:pointer;font-weight:700;font-size:.72rem}
.token-box{background:linear-gradient(135deg,rgba(0,224,255,.08),rgba(124,92,255,.08));border:1px solid rgba(0,224,255,.3);border-radius:14px;padding:18px;text-align:center;margin-bottom:14px}
.token-box .ttl{font-size:.75rem;color:var(--muted);margin-bottom:8px;font-weight:700;text-transform:uppercase;letter-spacing:1px}
.token-box .tk{font-family:'JetBrains Mono',monospace;font-size:1.4rem;font-weight:800;color:var(--cyan);letter-spacing:2px;margin-bottom:10px;direction:ltr}
.token-box .hint{font-size:.75rem;color:var(--muted);line-height:1.5}
.history-item{display:flex;justify-content:space-between;align-items:center;padding:11px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;font-size:.82rem}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(150%);background:var(--bg2);border:1px solid var(--border);padding:12px 20px;border-radius:12px;z-index:999;font-weight:600;font-size:.88rem;transition:transform .3s;max-width:calc(100% - 32px);text-align:center}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:300;display:none;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px)}
.modal-overlay.show{display:flex}
@media(max-width:600px){.modal-overlay{align-items:flex-end;padding:0}}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:20px;padding:22px;max-width:500px;width:100%;max-height:90vh;overflow-y:auto}
@media(max-width:600px){.modal{border-radius:20px 20px 0 0}}
.modal h3{margin-bottom:14px;font-size:1.05rem;font-weight:700}
.modal-actions{display:flex;gap:8px;margin-top:14px}
.modal-actions .btn{flex:1}
.install-btn{display:none;position:fixed;bottom:16px;right:16px;z-index:150;padding:11px 18px;border-radius:30px;border:none;cursor:pointer;background:var(--grad);color:#05070f;font-family:inherit;font-weight:800;font-size:.82rem;box-shadow:0 10px 30px rgba(124,92,255,.5)}
.install-btn.show{display:flex;align-items:center;gap:6px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
@media(max-width:500px){.grid-2{grid-template-columns:1fr}}
.task-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:10px}
.task-card .info{flex:1}
.task-card .title{font-weight:700;font-size:.92rem}
.task-card .reward{color:var(--success);font-size:.82rem;font-weight:700;margin-top:2px}
.task-card.done{opacity:.6}
.badge-claimed{background:rgba(0,255,163,.15);color:var(--success);padding:4px 10px;border-radius:20px;font-size:.72rem;font-weight:700}
.banner{background:linear-gradient(135deg,rgba(0,224,255,.08),rgba(124,92,255,.08));border:1px solid rgba(0,224,255,.25);border-radius:14px;padding:18px;text-align:center;margin-bottom:16px}
</style>
</head>
<body>

<!-- ═══════ WELCOME OVERLAY (shown once, requires click → creates key) ═══════ -->
<div id="welcome-overlay" class="modal-overlay" style="z-index:250">
<div class="modal" style="text-align:center">
<div style="font-size:3rem;margin-bottom:12px">👑</div>
<h3 style="margin-bottom:8px">مرحباً بك في Lux VIP</h3>
<p style="color:var(--muted);font-size:.88rem;margin-bottom:20px;line-height:1.6">
احصل على <b style="color:var(--cyan)">30 عملية مجانية</b> للبدء<br>
يُولَّد لك مفتاح شخصي تلقائياً عند الضغط على "ابدأ"
</p>
<button class="btn btn-primary btn-block btn-lg" onclick="getStarted()">🚀 ابدأ الآن</button>
<p style="color:var(--muted);font-size:.72rem;margin-top:12px">بالضغط أنت توافق على الاستخدام العادل</p>
</div>
</div>

<header>
<button class="ham" onclick="openDrawer()">☰</button>
<div class="h-title"><span id="h-title">Lux Studio</span><small id="h-sub">VIP v8</small></div>
<div style="display:flex;gap:8px"><a href="/owner" class="icon-btn" title="المالك">👑</a></div>
</header>

<div class="overlay" id="overlay" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer">
<div class="d-head">
<div class="d-logo">Lux<span>.</span>VIP</div>
<div class="d-user">
<div style="font-size:.68rem;color:var(--muted);margin-bottom:2px">رصيدك</div>
<div class="cr" id="d-credits">-</div>
<div class="tk" id="d-token">-</div>
</div>
</div>
<nav class="d-nav">
<button class="nav-item active" data-page="home" onclick="navigate('home')"><span class="ico">🏠</span>الرئيسية</button>
<button class="nav-item" data-page="remove-bg" onclick="navigate('remove-bg')"><span class="ico">🖼️</span>إزالة الخلفية</button>
<button class="nav-item" data-page="enhance" onclick="navigate('enhance')"><span class="ico">✨</span>تحسين الجودة</button>
<button class="nav-item" data-page="designer" onclick="navigate('designer')"><span class="ico">🎨</span>أدوات المصمم</button>
<button class="nav-item" data-page="tasks" onclick="navigate('tasks')"><span class="ico">🎁</span>المهام والجوائز</button>
<button class="nav-item" data-page="profile" onclick="navigate('profile')"><span class="ico">👤</span>ملفي الشخصي</button>
<button class="nav-item" data-page="api" onclick="navigate('api')"><span class="ico">🔑</span>مفاتيح API</button>
<button class="nav-item" data-page="history" onclick="navigate('history')"><span class="ico">📜</span>سجلي</button>
<button class="nav-item" data-page="settings" onclick="navigate('settings')"><span class="ico">⚙️</span>الإعدادات</button>
<button class="nav-item" data-page="help" onclick="navigate('help')"><span class="ico">❓</span>المساعدة</button>
</nav>
<div class="d-foot">Lux Studio VIP v8<br>© 2025</div>
</aside>

<button class="install-btn" id="install-btn" onclick="doInstall()">📱 تثبيت</button>

<main>

<!-- HOME -->
<section class="page" id="page-home">
<h1>👋 أهلاً بك</h1>
<p class="sub">21 أداة مصمم + مهام + جوائز</p>
<div class="stats-grid">
<div class="stat"><div class="num" id="st-credits">-</div><div class="lbl">نقطة</div></div>
<div class="stat"><div class="num" id="st-ops">-</div><div class="lbl">عملية</div></div>
<div class="stat"><div class="num" id="st-success">-</div><div class="lbl">ناجحة</div></div>
</div>
<div class="card">
<h3>⚡ إجراءات سريعة</h3>
<div class="grid-2" style="margin-top:10px">
<button class="btn btn-primary" onclick="navigate('remove-bg')">🖼️ إزالة خلفية</button>
<button class="btn btn-secondary" onclick="navigate('enhance')">✨ تحسين صورة</button>
<button class="btn btn-secondary" onclick="navigate('designer')">🎨 أدوات المصمم</button>
<button class="btn btn-secondary" onclick="navigate('tasks')">🎁 اكسب نقاط</button>
</div>
</div>
</section>

<!-- REMOVE BG -->
<section class="page hidden" id="page-remove-bg">
<h1>🖼️ إزالة الخلفية</h1>
<p class="sub">خلفية شفافة بدقة احترافية</p>
<div class="upload-zone" onclick="pickFile('remove-bg')">
<input type="file" accept="image/*" id="file-remove-bg">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>PNG · JPG · WEBP</p>
</div>
<div id="result-remove-bg" class="hidden"></div>
</section>

<!-- ENHANCE -->
<section class="page hidden" id="page-enhance">
<h1>✨ تحسين الجودة</h1>
<p class="sub">تكبير ×2 + حدة + ألوان</p>
<div class="upload-zone" onclick="pickFile('enhance')">
<input type="file" accept="image/*" id="file-enhance">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>PNG · JPG · WEBP</p>
</div>
<div id="result-enhance" class="hidden"></div>
</section>

<!-- DESIGNER -->
<section class="page hidden" id="page-designer">
<h1>🎨 أدوات المصمم</h1>
<p class="sub">19 أداة احترافية</p>
<div class="upload-zone" onclick="pickFile('designer')">
<input type="file" accept="image/*" id="file-designer">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>ستظهر جميع الأدوات</p>
</div>
<div id="designer-tools" class="hidden">
<div class="tools-grid">
<button class="tool-btn" onclick="runSimple('compress',{quality:70})"><span class="ic">📉</span>ضغط</button>
<button class="tool-btn" onclick="runSimple('grayscale')"><span class="ic">⚫</span>رمادي</button>
<button class="tool-btn" onclick="runSimple('sepia')"><span class="ic">🌅</span>سيبيا</button>
<button class="tool-btn" onclick="runSimple('invert')"><span class="ic">🌗</span>انعكاس</button>
<button class="tool-btn" onclick="runSimple('blur',{radius:5})"><span class="ic">💨</span>بلور</button>
<button class="tool-btn" onclick="runSimple('sharpen')"><span class="ic">🔪</span>حدّة</button>
<button class="tool-btn" onclick="runSimple('vintage')"><span class="ic">📷</span>فينتاج</button>
<button class="tool-btn" onclick="runSimple('warm')"><span class="ic">☀️</span>دافئ</button>
<button class="tool-btn" onclick="runSimple('cool')"><span class="ic">❄️</span>بارد</button>
<button class="tool-btn" onclick="runSimple('rotate',{angle:90})"><span class="ic">🔃</span>تدوير</button>
<button class="tool-btn" onclick="runSimple('flip',{direction:'h'})"><span class="ic">↔️</span>قلب</button>
<button class="tool-btn" onclick="runSimple('convert',{target:'png'})"><span class="ic">🔄</span>PNG</button>
<button class="tool-btn" onclick="runSimple('convert',{target:'webp'})"><span class="ic">🔄</span>WEBP</button>
<button class="tool-btn" onclick="openModal('modal-crop')"><span class="ic">✂️</span>قص</button>
<button class="tool-btn" onclick="openModal('modal-resize')"><span class="ic">📐</span>تحجيم</button>
<button class="tool-btn" onclick="openModal('modal-wm')"><span class="ic">💧</span>علامة</button>
<button class="tool-btn" onclick="openModal('modal-adjust')"><span class="ic">🎚️</span>ضبط</button>
<button class="tool-btn" onclick="openModal('modal-bg')"><span class="ic">🎨</span>خلفية</button>
<button class="tool-btn" onclick="runSimple('round-corners',{radius:40})"><span class="ic">⭕</span>زوايا</button>
<button class="tool-btn" onclick="runSimple('border',{thickness:20})"><span class="ic">🖼️</span>إطار</button>
</div>
</div>
<div id="result-designer" class="hidden"></div>
</section>

<!-- TASKS -->
<section class="page hidden" id="page-tasks">
<h1>🎁 المهام والجوائز</h1>
<p class="sub">اكسب نقاط مجانية</p>
<div class="banner"><div style="font-size:1.8rem;margin-bottom:6px">🏆</div><div style="font-weight:700">اجمع النقاط من المهام</div></div>
<div id="tasks-list"><div class="loading"><div class="spinner"></div><p>جاري التحميل...</p></div></div>
</section>

<!-- PROFILE — NEW PAGE -->
<section class="page hidden" id="page-profile">
<h1>👤 ملفي الشخصي</h1>
<p class="sub">بياناتك وتوكن الشحن</p>

<div class="token-box">
<div class="ttl">🎫 توكن المستخدم</div>
<div class="tk" id="profile-token">-</div>
<button class="btn btn-primary" onclick="copyToken()">📋 نسخ التوكن</button>
<div class="hint" style="margin-top:12px">
شارك هذا التوكن مع المالك لشحن رصيدك<br>
مثال: <b>LUX-A3B5-C7D9</b>
</div>
</div>

<div class="stats-grid">
<div class="stat"><div class="num" id="p-credits">-</div><div class="lbl">النقاط</div></div>
<div class="stat"><div class="num" id="p-ops">-</div><div class="lbl">العمليات</div></div>
<div class="stat"><div class="num" id="p-success">-</div><div class="lbl">الناجحة</div></div>
</div>

<div class="card">
<h3>🎟️ رابط الدعوة</h3>
<p style="color:var(--muted);font-size:.82rem;margin-bottom:10px">شارك الرابط → صديقك ينضم → تحصل على نقاط</p>
<div class="key-display">
<span id="invite-link" style="font-size:.68rem">-</span>
<button onclick="copyInvite()">📋</button>
</div>
<button class="btn btn-secondary btn-block" onclick="shareInvite()" style="margin-top:8px">📤 مشاركة</button>
</div>

<div class="card">
<h3>📅 تاريخ الانضمام</h3>
<p id="p-joined" style="color:var(--muted);font-size:.85rem">-</p>
</div>
</section>

<!-- API -->
<section class="page hidden" id="page-api">
<h1>🔑 مفاتيح API</h1>
<p class="sub">اربط خدماتك ببوتات أو مواقع</p>
<div class="card">
<h3>🔑 مفتاحك الحالي</h3>
<div class="key-display">
<span id="api-key-masked">-</span>
<button onclick="toggleKeyVis()" id="btn-toggle">👁</button>
<button onclick="copyApiKey()">📋</button>
</div>
<p style="color:var(--warn);font-size:.75rem">⚠️ احتفظ به بأمان</p>
</div>
<div class="card">
<h3>📖 كيفية الاستخدام</h3>
<div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:12px;font-family:'JetBrains Mono',monospace;font-size:.7rem;direction:ltr;text-align:left;overflow-x:auto;white-space:pre;color:#c9d1e0;margin-top:8px">
curl -X POST "YOUR/api/tool/remove-bg" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output result.png
</div>
</div>
</section>

<!-- HISTORY -->
<section class="page hidden" id="page-history">
<h1>📜 سجلي</h1>
<p class="sub">آخر 50 عملية</p>
<div class="card" id="hist-box"><div class="loading"><div class="spinner"></div><p>تحميل...</p></div></div>
</section>

<!-- SETTINGS -->
<section class="page hidden" id="page-settings">
<h1>⚙️ الإعدادات</h1>
<p class="sub">تخصيص التطبيق</p>
<div class="card">
<h3>🎨 المظهر</h3>
<div class="grid-2" style="margin-top:10px">
<button class="btn btn-secondary" onclick="setTheme('dark')">🌙 داكن</button>
<button class="btn btn-secondary" onclick="setTheme('light')">☀️ فاتح</button>
</div>
</div>
<div class="card">
<h3>🔄 المفتاح</h3>
<p style="color:var(--muted);font-size:.8rem;margin-top:6px">لن تتمكن من استرجاعه بعد المسح</p>
<button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="regenerateKey()">🔄 توليد مفتاح جديد</button>
</div>
<div class="card">
<h3>📱 تثبيت التطبيق</h3>
<button class="btn btn-primary btn-block" style="margin-top:10px" onclick="doInstall()">📱 تثبيت على الجوال</button>
</div>
</section>

<!-- HELP -->
<section class="page hidden" id="page-help">
<h1>❓ المساعدة</h1>
<p class="sub">دليل سريع</p>
<div class="card"><h3>🔑 المفتاح</h3><p style="color:var(--muted);font-size:.85rem;margin-top:6px">يتم إنشاؤه عند ضغط "ابدأ" — تجده في صفحة API.</p></div>
<div class="card"><h3>🎫 توكن الشحن</h3><p style="color:var(--muted);font-size:.85rem;margin-top:6px">في صفحة "ملفي الشخصي" — شاركه مع المالك لشحن النقاط.</p></div>
<div class="card"><h3>💳 النقاط</h3><p style="color:var(--muted);font-size:.85rem;margin-top:6px">كل عملية = نقطة. اكسب المزيد من صفحة المهام.</p></div>
<div class="card"><h3>⚡ بطء أول عملية</h3><p style="color:var(--muted);font-size:.85rem;margin-top:6px">موديل إزالة الخلفية يُحمَّل مرة واحدة (~60ث) ثم يصبح سريعاً.</p></div>
</section>

</main>

<!-- MODALS -->
<div class="modal-overlay" id="modal-crop"><div class="modal">
<h3>✂️ قص بنسبة</h3>
<div class="form-group"><label>النسبة</label>
<select class="form-input" id="crop-ratio"><option value="1:1">1:1</option><option value="4:3">4:3</option><option value="16:9">16:9</option><option value="9:16">9:16</option><option value="3:2">3:2</option></select></div>
<div class="modal-actions"><button class="btn btn-secondary" onclick="closeModal('modal-crop')">إلغاء</button><button class="btn btn-primary" onclick="doCrop()">تطبيق</button></div>
</div></div>

<div class="modal-overlay" id="modal-resize"><div class="modal">
<h3>📐 تحجيم</h3>
<div class="grid-2"><div class="form-group"><label>عرض (px)</label><input type="number" class="form-input" id="rz-w"></div><div class="form-group"><label>ارتفاع (px)</label><input type="number" class="form-input" id="rz-h"></div></div>
<div class="modal-actions"><button class="btn btn-secondary" onclick="closeModal('modal-resize')">إلغاء</button><button class="btn btn-primary" onclick="doResize()">تطبيق</button></div>
</div></div>

<div class="modal-overlay" id="modal-wm"><div class="modal">
<h3>💧 علامة مائية</h3>
<div class="form-group"><label>النص</label><input type="text" class="form-input" id="wm-text" value="Lux Studio"></div>
<div class="grid-2"><div class="form-group"><label>الموضع</label><select class="form-input" id="wm-pos"><option value="br">أسفل يمين</option><option value="bl">أسفل يسار</option><option value="tr">أعلى يمين</option><option value="tl">أعلى يسار</option><option value="c">وسط</option></select></div><div class="form-group"><label>شفافية</label><input type="number" class="form-input" id="wm-op" value="60" min="10" max="100"></div></div>
<div class="modal-actions"><button class="btn btn-secondary" onclick="closeModal('modal-wm')">إلغاء</button><button class="btn btn-primary" onclick="doWm()">تطبيق</button></div>
</div></div>

<div class="modal-overlay" id="modal-adjust"><div class="modal">
<h3>🎚️ ضبط</h3>
<div class="form-group"><label>سطوع: <span id="adj-bv">0</span></label><input type="range" id="adj-b" min="-50" max="50" value="0" style="width:100%" oninput="document.getElementById('adj-bv').textContent=this.value"></div>
<div class="form-group"><label>تباين: <span id="adj-cv">0</span></label><input type="range" id="adj-c" min="-50" max="50" value="0" style="width:100%" oninput="document.getElementById('adj-cv').textContent=this.value"></div>
<div class="form-group"><label>تشبع: <span id="adj-sv">0</span></label><input type="range" id="adj-s" min="-50" max="50" value="0" style="width:100%" oninput="document.getElementById('adj-sv').textContent=this.value"></div>
<div class="modal-actions"><button class="btn btn-secondary" onclick="closeModal('modal-adjust')">إلغاء</button><button class="btn btn-primary" onclick="doAdjust()">تطبيق</button></div>
</div></div>

<div class="modal-overlay" id="modal-bg"><div class="modal">
<h3>🎨 خلفية لون</h3>
<div class="form-group"><label>اللون</label><input type="color" class="form-input" id="bg-color" value="#ffffff" style="height:48px;padding:3px"></div>
<div class="modal-actions"><button class="btn btn-secondary" onclick="closeModal('modal-bg')">إلغاء</button><button class="btn btn-primary" onclick="doBg()">تطبيق</button></div>
</div></div>

<div class="toast" id="toast"></div>

<script>
// ═══════════════ STATE ═══════════════
const state = {
  key: localStorage.getItem('lux_api_key') || null,
  token: localStorage.getItem('lux_user_token') || null,
  inviteCode: localStorage.getItem('lux_invite_code') || null,
  currentPage: 'home',
  currentFile: null,
  currentSource: null,
  theme: localStorage.getItem('lux_theme') || 'dark',
  keyVisible: false,
  deferredPrompt: null,
};

const PAGES = {
  home:{t:'الرئيسية',s:'Lux Studio VIP v8'},
  'remove-bg':{t:'إزالة الخلفية',s:'شفافية احترافية'},
  enhance:{t:'تحسين الجودة',s:'تكبير ×2'},
  designer:{t:'أدوات المصمم',s:'19 أداة'},
  tasks:{t:'المهام والجوائز',s:'اكسب نقاط'},
  profile:{t:'ملفي الشخصي',s:'توكن الشحن'},
  api:{t:'مفاتيح API',s:'اربط خدماتك'},
  history:{t:'سجلي',s:'عملياتك'},
  settings:{t:'الإعدادات',s:'تخصيص'},
  help:{t:'المساعدة',s:'دليل سريع'},
};

function navigate(page){
  if(!PAGES[page]) page='home';
  state.currentPage=page;
  document.querySelectorAll('.page').forEach(p=>p.classList.add('hidden'));
  const el=document.getElementById('page-'+page);
  if(el) el.classList.remove('hidden');
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.page===page));
  document.getElementById('h-title').textContent=PAGES[page].t;
  document.getElementById('h-sub').textContent=PAGES[page].s;
  closeDrawer();
  window.scrollTo({top:0,behavior:'smooth'});
  if(page==='history') loadHistory();
  if(page==='tasks') loadTasks();
  if(page==='profile') loadProfile();
  history.replaceState(null,'','#/'+page);
}
function openDrawer(){document.getElementById('drawer').classList.add('open');document.getElementById('overlay').classList.add('open')}
function closeDrawer(){document.getElementById('drawer').classList.remove('open');document.getElementById('overlay').classList.remove('open')}

function toast(m,t='success'){const el=document.getElementById('toast');el.textContent=m;el.className='toast show '+t;clearTimeout(el._t);el._t=setTimeout(()=>el.className='toast '+t,3500)}

function openModal(id){if(!state.currentFile)return toast('❌ اختر صورة أولاً','error');document.getElementById(id).classList.add('show')}
function closeModal(id){document.getElementById(id).classList.remove('show')}
document.querySelectorAll('.modal-overlay').forEach(m=>m.addEventListener('click',e=>{if(e.target===m&&m.id!=='welcome-overlay')m.classList.remove('show')}));

function setTheme(t){state.theme=t;localStorage.setItem('lux_theme',t);applyTheme();toast(t==='light'?'☀️ فاتح':'🌙 داكن')}
function applyTheme(){document.body.classList.toggle('light',state.theme==='light')}

// ═══════════════ KEY — NO AUTO-CREATE ON LOAD ═══════════════
function hideWelcome(){
  const w = document.getElementById('welcome-overlay');
  if(w) w.classList.remove('show');
}

async function getStarted(){
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ جاري الإنشاء...';
  const k = await ensureKey();
  if(k){
    hideWelcome();
    toast('🎉 تم إنشاء حسابك! 30 نقطة في رصيدك');
    await refreshUser();
    navigate('home');
  } else {
    btn.disabled = false;
    btn.textContent = '🚀 ابدأ الآن';
  }
}

async function ensureKey(invite=''){
  if(state.key) return state.key;
  try{
    const url='/api/keys/new'+(invite?'?invite='+encodeURIComponent(invite):'');
    const r=await fetch(url,{method:'POST'});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'فشل');
    state.key=d.api_key;
    state.token=d.user_token;
    state.inviteCode=d.invite_code;
    localStorage.setItem('lux_api_key',d.api_key);
    localStorage.setItem('lux_user_token',d.user_token||'');
    localStorage.setItem('lux_invite_code',d.invite_code||'');
    return state.key;
  }catch(e){
    toast('❌ '+e.message,'error');
    return null;
  }
}

async function refreshUser(){
  if(!state.key) return;
  try{
    const r=await fetch('/api/me',{headers:{'X-API-Key':state.key}});
    if(r.status===401){
      localStorage.removeItem('lux_api_key');
      localStorage.removeItem('lux_user_token');
      state.key=null; state.token=null;
      hideWelcome();
      return;
    }
    if(!r.ok) return;
    const d=await r.json();
    document.getElementById('st-credits').textContent=d.credits;
    document.getElementById('st-ops').textContent=d.total_operations;
    document.getElementById('st-success').textContent=d.successful;
    document.getElementById('d-credits').textContent=d.credits;
    document.getElementById('d-token').textContent=d.user_token||'-';
    document.getElementById('api-key-masked').textContent=state.keyVisible?state.key:maskKey(state.key);
    state.token=d.user_token; state.inviteCode=d.invite_code;
    if(d.user_token) localStorage.setItem('lux_user_token',d.user_token);
    if(d.invite_code) localStorage.setItem('lux_invite_code',d.invite_code);
    hideWelcome();
  }catch(e){}
}

function maskKey(k){return k?k.slice(0,10)+'••••••'+k.slice(-4):'-'}
function toggleKeyVis(){state.keyVisible=!state.keyVisible;document.getElementById('api-key-masked').textContent=state.keyVisible?state.key:maskKey(state.key);document.getElementById('btn-toggle').textContent=state.keyVisible?'🙈':'👁'}
function copyApiKey(){if(state.key)navigator.clipboard.writeText(state.key).then(()=>toast('📋 تم النسخ'))}
function copyToken(){
  const tk = state.token || localStorage.getItem('lux_user_token');
  if(tk) navigator.clipboard.writeText(tk).then(()=>toast('📋 تم نسخ التوكن'));
  else toast('❌ لا يوجد توكن','error');
}
async function regenerateKey(){
  if(!confirm('توليد مفتاح جديد؟ المفتاح القديم سيتوقف.'))return;
  state.key=null; state.token=null;
  localStorage.removeItem('lux_api_key');
  localStorage.removeItem('lux_user_token');
  const k=await ensureKey();
  if(k){toast('✅ مفتاح جديد');await refreshUser();}
}

// ═══════════════ PROFILE ═══════════════
async function loadProfile(){
  if(!state.key){await ensureKey();}
  if(!state.key) return;
  try{
    const r=await fetch('/api/me',{headers:{'X-API-Key':state.key}});
    if(!r.ok) return;
    const d=await r.json();
    state.token=d.user_token;
    state.inviteCode=d.invite_code;
    document.getElementById('profile-token').textContent=d.user_token||'-';
    document.getElementById('p-credits').textContent=d.credits;
    document.getElementById('p-ops').textContent=d.total_operations;
    document.getElementById('p-success').textContent=d.successful;
    const link = location.origin + '/?invite=' + (d.invite_code||'');
    document.getElementById('invite-link').textContent = link;
    const dt = d.created_at ? new Date(d.created_at).toLocaleDateString('ar-SA') : '-';
    document.getElementById('p-joined').textContent = dt;
  }catch(e){}
}
function copyInvite(){
  const link = document.getElementById('invite-link').textContent;
  navigator.clipboard.writeText(link).then(()=>toast('📋 تم النسخ'));
}
function shareInvite(){
  const link = document.getElementById('invite-link').textContent;
  if(navigator.share){
    navigator.share({title:'Lux Studio VIP',text:'انضم إلي واحصل على أدوات تصميم مجانية!',url:link}).catch(()=>{});
  } else {
    navigator.clipboard.writeText(link).then(()=>toast('📋 تم النسخ — شاركه'));
  }
}

// ═══════════════ FILE ═══════════════
function pickFile(src){state.currentSource=src;document.getElementById('file-'+src).click()}
['remove-bg','enhance','designer'].forEach(src=>{
  document.getElementById('file-'+src).addEventListener('change',e=>{
    if(e.target.files.length)handleFile(e.target.files[0],src);
    e.target.value='';
  });
});

function handleFile(file,src){
  if(!file.type.startsWith('image/'))return toast('❌ ليس صورة','error');
  if(file.size>15*1024*1024)return toast('❌ أكبر من 15MB','error');
  state.currentFile=file;state.currentSource=src;
  if(src==='designer'){
    document.getElementById('designer-tools').classList.remove('hidden');
    document.getElementById('result-designer').classList.add('hidden');
    toast('📸 اختر أداة');
  }else if(src==='remove-bg')executeTool('remove-bg',{},'result-remove-bg');
  else if(src==='enhance')executeTool('enhance',{},'result-enhance');
}

async function executeTool(name,params,resultId){
  if(!state.currentFile)return toast('❌ اختر صورة','error');
  // Auto-create key on user action (not on page load)
  if(!state.key){
    const k=await ensureKey();
    if(!k) return toast('❌ تعذر إنشاء المفتاح','error');
    hideWelcome();
  }
  const container=document.getElementById(resultId);
  container.classList.remove('hidden');
  container.innerHTML='<div class="card"><div class="loading"><div class="spinner"></div><p>⏳ جاري المعالجة...</p></div></div>';
  const form=new FormData();form.append('file',state.currentFile);
  const qs=new URLSearchParams(params).toString();
  const url='/api/tool/'+name+(qs?'?'+qs:'');
  const t0=performance.now();
  try{
    const r=await fetch(url,{method:'POST',headers:{'X-API-Key':state.key},body:form});
    if(!r.ok){
      const err=await r.json().catch(()=>({}));
      if(r.status===401){
        localStorage.removeItem('lux_api_key');state.key=null;
        const k=await ensureKey();
        if(k) return executeTool(name,params,resultId);
      }
      throw new Error(err.detail||`خطأ ${r.status}`);
    }
    const blob=await r.blob();
    const objUrl=URL.createObjectURL(blob);
    const ext=blob.type==='image/png'?'png':blob.type==='image/webp'?'webp':'jpg';
    const reader=new FileReader();
    reader.onload=e=>{
      container.innerHTML=`
        <div class="preview-row">
          <div class="img-box"><div class="label">الأصلية</div><img src="${e.target.result}"></div>
          <div class="img-box"><div class="label">النتيجة</div><div class="${name==='remove-bg'?'checker':''}"><img src="${objUrl}"></div></div>
        </div>
        <div class="grid-2">
          <a href="${objUrl}" download="lux_${name}.${ext}" class="btn btn-primary">⬇️ تحميل</a>
          <button class="btn btn-secondary" onclick="resetPage('${state.currentSource}')">🔄 صورة جديدة</button>
        </div>`;
    };
    reader.readAsDataURL(state.currentFile);
    toast(`✅ ${Math.round(performance.now()-t0)}ms`);
    await refreshUser();
  }catch(e){
    container.innerHTML=`<div class="card"><div style="color:var(--danger);text-align:center;padding:18px">❌ ${e.message}</div></div>`;
    toast('❌ '+e.message,'error');
  }
}

function runSimple(name,params={}){
  if(!state.currentFile)return toast('❌ اختر صورة','error');
  executeTool(name,params,'result-designer');
}
function resetPage(src){
  state.currentFile=null;
  document.getElementById('file-'+src).value='';
  if(src==='designer'){document.getElementById('designer-tools').classList.add('hidden');document.getElementById('result-designer').classList.add('hidden')}
  else if(src==='remove-bg')document.getElementById('result-remove-bg').classList.add('hidden');
  else if(src==='enhance')document.getElementById('result-enhance').classList.add('hidden');
}

function doCrop(){const r=document.getElementById('crop-ratio').value;closeModal('modal-crop');runSimple('crop',{ratio:r})}
function doResize(){const w=document.getElementById('rz-w').value,h=document.getElementById('rz-h').value;if(!w&&!h)return toast('❌ أدخل قيمة','error');closeModal('modal-resize');const p={};if(w)p.width=+w;if(h)p.height=+h;runSimple('resize',p)}
function doWm(){const t=document.getElementById('wm-text').value||'Lux Studio';const p=document.getElementById('wm-pos').value;const o=+document.getElementById('wm-op').value;closeModal('modal-wm');runSimple('watermark',{text:t,position:p,opacity:o})}
function doAdjust(){const b=+document.getElementById('adj-b').value,c=+document.getElementById('adj-c').value,s=+document.getElementById('adj-s').value;if(!b&&!c&&!s)return toast('⚠️ لا تغيير','error');closeModal('modal-adjust');runSimple('adjust',{brightness:b,contrast:c,saturation:s})}
function doBg(){const c=document.getElementById('bg-color').value;closeModal('modal-bg');runSimple('bg-color',{color:c})}

// ═══════════════ TASKS ═══════════════
async function loadTasks(){
  const el=document.getElementById('tasks-list');
  if(!state.key){const k=await ensureKey();if(!k)return el.innerHTML='<div class="card">تعذر الاتصال</div>'}
  el.innerHTML='<div class="loading"><div class="spinner"></div><p>تحميل...</p></div>';
  try{
    const r=await fetch('/api/tasks',{headers:{'X-API-Key':state.key}});
    if(!r.ok) throw new Error();
    const d=await r.json();
    let html='';
    html+=renderTask('daily_checkin',d.daily_checkin,'📅','checkin');
    html+=renderTask('share_app',d.share_app,'📤','share');
    d.links.forEach(lk=>{
      html+=`<div class="task-card ${lk.claimed?'done':''}">
        <div class="info"><div class="title">${lk.icon||'🔗'} ${lk.title}</div><div class="reward">+${lk.reward} نقطة</div></div>
        ${lk.claimed?'<span class="badge-claimed">✓ تم</span>':`<a href="${lk.url}" target="_blank" class="btn btn-primary" onclick="setTimeout(()=>claimLink('${lk.id}'),500)">اشترك</a>`}
      </div>`;
    });
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--danger)">فشل التحميل</div>'}
}

function renderTask(id,cfg,icon,action){
  return `<div class="task-card ${cfg.claimed?'done':''}">
    <div class="info"><div class="title">${icon} ${cfg.label||id}</div><div class="reward">+${cfg.reward} نقطة</div></div>
    ${cfg.claimed?'<span class="badge-claimed">✓ تم</span>':`<button class="btn btn-primary" onclick="claimTask('${action}')">استلام</button>`}
  </div>`;
}

async function claimTask(action){
  if(!state.key)return toast('❌ مفتاح مفقود','error');
  try{
    const endpoint=action==='checkin'?'/api/tasks/checkin':action==='share'?'/api/tasks/share':null;
    if(!endpoint)return;
    const r=await fetch(endpoint,{method:'POST',headers:{'X-API-Key':state.key}});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'فشل');
    toast('🎉 '+d.message);
    await refreshUser();
    await loadTasks();
  }catch(e){toast('❌ '+e.message,'error')}
}

async function claimLink(linkId){
  if(!state.key)return;
  try{
    const f=new FormData();f.append('link_id',linkId);
    const r=await fetch('/api/tasks/claim-link',{method:'POST',headers:{'X-API-Key':state.key},body:f});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'فشل');
    toast('🎉 '+d.message);
    await refreshUser();
    await loadTasks();
  }catch(e){}
}

// ═══════════════ HISTORY ═══════════════
async function loadHistory(){
  const el=document.getElementById('hist-box');
  if(!state.key) return el.innerHTML='<div style="text-align:center;padding:20px;color:var(--muted)">لا يوجد سجل</div>';
  el.innerHTML='<div class="loading"><div class="spinner"></div><p>تحميل...</p></div>';
  try{
    const r=await fetch('/api/history',{headers:{'X-API-Key':state.key}});
    if(!r.ok) throw new Error();
    const d=await r.json();
    if(!d.items||!d.items.length){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--muted)">لا يوجد سجل بعد</div>';return}
    el.innerHTML=d.items.slice(0,30).map(x=>`
      <div class="history-item">
        <span style="font-weight:700">${x.action||'?'}</span>
        <span style="color:${x.status==='success'?'var(--success)':'var(--danger)'}">${x.status==='success'?'✅':'❌'}</span>
        <span style="color:var(--muted);font-size:.72rem">${(x.created_at||'').slice(0,16).replace('T',' ')}</span>
      </div>
    `).join('');
  }catch(e){el.innerHTML='<div style="color:var(--danger);text-align:center;padding:20px">فشل</div>'}
}

// ═══════════════ PWA ═══════════════
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();state.deferredPrompt=e;document.getElementById('install-btn').classList.add('show')});
function doInstall(){if(!state.deferredPrompt)return toast('افتح في Chrome على جوالك','error');state.deferredPrompt.prompt();state.deferredPrompt.userChoice.then(()=>{state.deferredPrompt=null;document.getElementById('install-btn').classList.remove('show')})}

// ═══════════════ INIT — NO AUTO-CREATE ON LOAD ═══════════════
window.addEventListener('DOMContentLoaded', async()=>{
  applyTheme();
  if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});
  // CRITICAL: Do NOT auto-create key on page load
  if(state.key){
    hideWelcome();
    await refreshUser();
  } else {
    // Show welcome overlay with "Start" button
    document.getElementById('welcome-overlay').classList.add('show');
  }
  const page=location.hash.replace('#/','')||'home';
  navigate(page);
  setInterval(()=>{ if(state.key) refreshUser(); }, 60000);
});
window.addEventListener('hashchange',()=>{const p=location.hash.replace('#/','')||'home';if(p!==state.currentPage)navigate(p)});
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
#  OWNER LOGIN / DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════
OWNER_LOGIN_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Owner — Lux VIP</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'Segoe UI',sans-serif;background:#05070f;color:#e8ecf4;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background-image:radial-gradient(circle at 20% 20%,rgba(0,224,255,.12),transparent 40%),radial-gradient(circle at 80% 30%,rgba(124,92,255,.14),transparent 40%)}.card{background:rgba(255,255,255,.04);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:44px 36px;max-width:400px;width:100%;text-align:center;box-shadow:0 30px 80px rgba(0,0,0,.6)}.icon{font-size:3.2rem;margin-bottom:14px}h1{font-size:1.5rem;margin-bottom:6px;background:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95);-webkit-background-clip:text;background-clip:text;color:transparent}.sub{color:#8a94a8;font-size:.85rem;margin-bottom:26px}label{display:block;text-align:right;font-weight:600;margin-bottom:6px;font-size:.82rem;color:#8a94a8}input{width:100%;padding:13px 15px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:#05070f;color:#e8ecf4;font-size:1rem;margin-bottom:18px}input:focus{outline:none;border-color:#00e0ff}button{width:100%;padding:13px;border:none;border-radius:12px;background:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95);color:#05070f;font-weight:800;cursor:pointer;font-size:.95rem}.back{display:inline-block;margin-top:20px;color:#8a94a8;text-decoration:none;font-size:.82rem}</style>
</head><body>
<div class="card"><div class="icon">👑</div><h1>لوحة المالك</h1><p class="sub">Lux Studio VIP — Owner</p>
<form method="post" action="/owner/login"><label>كلمة المرور</label><input type="password" name="password" required autofocus placeholder="••••••••"><button>🔓 دخول</button></form>
<a href="/" class="back">← العودة للتطبيق</a></div>
</body></html>"""


OWNER_DASHBOARD_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Owner — Lux VIP</title>
<style>
:root{--bg:#05070f;--bg2:#0a0e1a;--surface:rgba(255,255,255,.04);--border:rgba(255,255,255,.08);--text:#e8ecf4;--muted:#8a94a8;--cyan:#00e0ff;--success:#00ffa3;--danger:#ff4d6d;--grad:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(circle at 15% 20%,rgba(0,224,255,.08),transparent 35%),radial-gradient(circle at 85% 15%,rgba(124,92,255,.10),transparent 35%)}
header{background:rgba(5,7,15,.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:14px 22px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10;flex-wrap:wrap;gap:8px}
.logo{font-weight:900;font-size:1.15rem}.logo span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.nav-tabs{display:flex;gap:6px;flex-wrap:wrap}
.nav-tab{background:transparent;border:1px solid var(--border);color:var(--muted);padding:7px 14px;border-radius:8px;cursor:pointer;font-family:inherit;font-weight:700;font-size:.8rem}
.nav-tab.active{background:var(--surface);color:var(--cyan);border-color:var(--cyan)}
.logout{background:rgba(255,77,109,.15);border:1px solid rgba(255,77,109,.3);color:var(--danger);padding:7px 14px;border-radius:8px;cursor:pointer;font-weight:700;font-size:.8rem}
main{max-width:1200px;margin:0 auto;padding:22px}
h1{font-size:1.4rem;margin-bottom:18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:22px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px;text-align:center}
.stat .num{font-size:1.5rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:monospace}
.stat .lbl{color:var(--muted);font-size:.72rem;margin-top:2px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:14px;overflow-x:auto}
.charge-box{background:linear-gradient(135deg,rgba(0,224,255,.08),rgba(124,92,255,.08));border:1px solid rgba(0,224,255,.3);border-radius:14px;padding:20px;margin-bottom:22px}
.charge-box h3{font-size:1.1rem;margin-bottom:12px}
.charge-form{display:grid;grid-template-columns:2fr 1fr auto;gap:10px;align-items:end}
@media(max-width:700px){.charge-form{grid-template-columns:1fr}}
.charge-form input{padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:1rem;font-weight:700}
.charge-form input:focus{outline:none;border-color:var(--cyan)}
.charge-form button{padding:12px 24px;background:var(--grad);color:var(--bg);border:none;border-radius:10px;font-weight:800;cursor:pointer;font-size:.9rem;white-space:nowrap}
.charge-result{margin-top:14px;padding:12px;border-radius:10px;font-weight:700;font-size:.88rem;display:none}
.charge-result.ok{display:block;background:rgba(0,255,163,.15);color:var(--success);border:1px solid rgba(0,255,163,.3)}
.charge-result.err{display:block;background:rgba(255,77,109,.15);color:var(--danger);border:1px solid rgba(255,77,109,.3)}
table{width:100%;border-collapse:collapse;font-size:.78rem;min-width:700px}
th{text-align:right;padding:9px 6px;color:var(--muted);font-size:.68rem;text-transform:uppercase;border-bottom:1px solid var(--border)}
td{padding:9px 6px;border-bottom:1px solid rgba(255,255,255,.03)}
.mono{font-family:monospace;font-size:.7rem;color:var(--cyan)}
.token-cell{font-family:monospace;font-size:.72rem;font-weight:700;color:var(--cyan);background:rgba(0,224,255,.08);padding:2px 6px;border-radius:4px}
.btn{padding:5px 10px;border:none;border-radius:6px;font-size:.68rem;font-weight:700;cursor:pointer;margin:1px}
.btn-add{background:rgba(0,255,163,.15);color:var(--success);border:1px solid rgba(0,255,163,.3)}
.btn-ban{background:rgba(255,176,32,.15);color:#ffb020;border:1px solid rgba(255,176,32,.3)}
.panel{display:none}.panel.active{display:block}
.form-group{margin-bottom:12px}
.form-group label{display:block;margin-bottom:5px;font-weight:600;font-size:.8rem;color:var(--muted)}
.form-input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--text);font-family:inherit;font-size:.85rem}
.form-input:focus{outline:none;border-color:var(--cyan)}
.link-row{display:grid;grid-template-columns:2fr 3fr 1fr 1fr auto;gap:6px;margin-bottom:8px;align-items:center}
@media(max-width:700px){.link-row{grid-template-columns:1fr;gap:4px;padding:10px;border:1px solid var(--border);border-radius:8px;margin-bottom:10px}}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(150%);background:var(--bg2);border:1px solid var(--border);padding:12px 20px;border-radius:12px;z-index:99;font-weight:600;font-size:.85rem;transition:transform .3s}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
.search-input{width:100%;padding:12px 16px;border-radius:10px;border:1px solid var(--border);background:var(--bg2);color:var(--text);font-size:.9rem;margin-bottom:12px}
</style></head><body>
<header>
<div class="logo">👑 Lux <span>VIP</span> Owner</div>
<div class="nav-tabs">
<button class="nav-tab active" onclick="tab('stats')">📊 إحصائيات</button>
<button class="nav-tab" onclick="tab('charge')">💳 شحن</button>
<button class="nav-tab" onclick="tab('users')">👥 المستخدمون</button>
<button class="nav-tab" onclick="tab('tasks')">🎁 المهام</button>
<button class="nav-tab" onclick="tab('pass')">🔑 كلمة المرور</button>
</div>
<form method="post" action="/owner/logout" style="margin:0"><button class="logout">🚪 خروج</button></form>
</header>
<main>

<div class="panel active" id="p-stats">
<h1>📊 إحصائيات المنصة</h1>
<div class="stats" id="stats-box"><div class="stat"><div class="num">...</div><div class="lbl">جارٍ التحميل</div></div></div>
</div>

<div class="panel" id="p-charge">
<h1>💳 شحن رصيد بتوكن</h1>
<div class="charge-box">
<h3>🎫 ابحث بالتوكن (LUX-XXXX-XXXX)</h3>
<form class="charge-form" onsubmit="chargeByToken(event)">
<input type="text" id="charge-token" placeholder="LUX-A3B5-C7D9" required style="text-transform:uppercase">
<input type="number" id="charge-amount" placeholder="عدد النقاط" required>
<button type="submit">💳 شحن</button>
</form>
<div id="charge-result" class="charge-result"></div>
</div>
</div>

<div class="panel" id="p-users">
<h1>👥 المستخدمون</h1>
<input type="text" class="search-input" id="user-search" placeholder="🔍 ابحث بالتوكن أو ID..." oninput="renderUsers()">
<div class="card" id="users-box">جارٍ التحميل...</div>
</div>

<div class="panel" id="p-tasks">
<h1>🎁 إعدادات المهام</h1>
<div class="card">
<h3 style="margin-bottom:12px">💎 جوائز المهام</h3>
<div class="form-group"><label>جائزة الزيارة اليومية</label><input type="number" class="form-input" id="t-daily" value="3"></div>
<div class="form-group"><label>جائزة المشاركة</label><input type="number" class="form-input" id="t-share" value="5"></div>
<div class="form-group"><label>جائزة الدعوة</label><input type="number" class="form-input" id="t-invite" value="15"></div>
</div>
<div class="card">
<h3 style="margin-bottom:10px">🔗 روابط الاشتراك</h3>
<p style="color:var(--muted);font-size:.78rem;margin-bottom:10px">كل رابط يمنح المستخدم النقاط عند الاشتراك</p>
<div id="links-container"></div>
<button class="btn btn-add" onclick="addLink()" style="margin-top:10px;padding:8px 16px">+ إضافة رابط</button>
</div>
<button class="btn btn-add" onclick="saveConfig()" style="padding:12px 28px;font-size:.9rem;margin-top:10px">💾 حفظ الإعدادات</button>
</div>

<div class="panel" id="p-pass">
<h1>🔑 تغيير كلمة المرور</h1>
<div class="card" style="max-width:500px">
<div class="form-group"><label>كلمة المرور الحالية</label><input type="password" class="form-input" id="old-pass"></div>
<div class="form-group"><label>كلمة المرور الجديدة</label><input type="password" class="form-input" id="new-pass"></div>
<button class="btn btn-add" onclick="changePass()" style="padding:12px 24px;font-size:.85rem;margin-top:8px">💾 حفظ</button>
</div>
</div>

</main>
<div class="toast" id="toast"></div>
<script>
let usersCache = [];
function toast(m,t='success'){const el=document.getElementById('toast');el.textContent=m;el.className='toast show '+t;clearTimeout(el._t);el._t=setTimeout(()=>el.className='toast '+t,3000)}
function tab(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('p-'+name).classList.add('active');
  document.querySelectorAll('.nav-tab').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  if(name==='stats')loadStats();
  if(name==='users')loadUsers();
  if(name==='tasks')loadConfig();
}

async function loadStats(){
  try{
    const r=await fetch('/api/owner/stats');const d=await r.json();
    document.getElementById('stats-box').innerHTML=`
      <div class="stat"><div class="num">${d.users}</div><div class="lbl">مستخدم</div></div>
      <div class="stat"><div class="num">${d.keys_active}</div><div class="lbl">مفتاح نشط</div></div>
      <div class="stat"><div class="num">${d.operations}</div><div class="lbl">عملية</div></div>
      <div class="stat"><div class="num">${d.successful}</div><div class="lbl">ناجحة</div></div>
      <div class="stat"><div class="num">${d.banned}</div><div class="lbl">محظور</div></div>
      <div class="stat"><div class="num">${d.total_credits}</div><div class="lbl">إجمالي نقاط</div></div>`;
  }catch(e){toast('فشل','error')}
}

async function chargeByToken(ev){
  ev.preventDefault();
  const token = document.getElementById('charge-token').value.trim();
  const amount = parseInt(document.getElementById('charge-amount').value);
  const res = document.getElementById('charge-result');
  if(!token||!amount){res.className='charge-result err';res.textContent='❌ املأ الحقلين';return}
  try{
    const f=new FormData();f.append('user_token',token);f.append('amount',amount);
    const r=await fetch('/api/owner/charge-by-token',{method:'POST',body:f});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'فشل');
    res.className='charge-result ok';
    res.textContent=`✅ تم! المستخدم ${d.user_token} — رصيده الآن ${d.new_credits} نقطة`;
    document.getElementById('charge-token').value='';
    document.getElementById('charge-amount').value='';
  }catch(e){res.className='charge-result err';res.textContent='❌ '+e.message}
}

async function loadUsers(){
  try{
    const r=await fetch('/api/owner/users');const d=await r.json();
    usersCache = d.items || [];
    renderUsers();
  }catch(e){toast('فشل','error')}
}
function renderUsers(){
  const q = (document.getElementById('user-search').value || '').toUpperCase();
  const items = usersCache.filter(u =>
    !q || (u.user_token||'').toUpperCase().includes(q) || String(u.id).toUpperCase().includes(q) || String(u.ip).includes(q)
  );
  const el=document.getElementById('users-box');
  if(!items.length){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--muted)">لا يوجد مستخدمون</div>';return}
  el.innerHTML=`<table><thead><tr><th>التوكن</th><th>ID</th><th>IP</th><th>نقاط</th><th>عمليات</th><th>UA</th><th>الحالة</th><th>إجراءات</th></tr></thead><tbody>${items.map(x=>`<tr>
    <td><span class="token-cell">${x.user_token||'-'}</span></td>
    <td class="mono">${x.id.slice(0,8)}</td>
    <td style="color:var(--muted);font-size:.68rem">${x.ip}</td>
    <td><strong style="color:var(--cyan)">${x.credits}</strong></td>
    <td>${x.usage_count}</td>
    <td style="color:var(--muted);font-size:.65rem;max-width:120px;overflow:hidden;text-overflow:ellipsis">${x.ua||'-'}</td>
    <td>${x.is_banned?'<span style="color:var(--danger)">محظور</span>':'<span style="color:var(--success)">نشط</span>'}</td>
    <td><button class="btn btn-add" onclick="addCred('${x.id}')">+ نقاط</button><button class="btn btn-ban" onclick="toggleBan('${x.id}',${!x.is_banned})">${x.is_banned?'فك':'حظر'}</button></td>
  </tr>`).join('')}</tbody></table>`;
}

async function addCred(id){const v=prompt('عدد النقاط (سالب للخصم):');if(!v)return;const f=new FormData();f.append('user_id',id);f.append('amount',v);const r=await fetch('/api/owner/user/credits',{method:'POST',body:f});if(r.ok){toast('✅');loadUsers();loadStats()}else toast('❌','error')}
async function toggleBan(id,b){if(!confirm(b?'حظر؟':'فك؟'))return;const f=new FormData();f.append('user_id',id);f.append('ban',b?'true':'false');const r=await fetch('/api/owner/user/ban',{method:'POST',body:f});if(r.ok){toast('✅');loadUsers()}else toast('❌','error')}

let linksState=[];
async function loadConfig(){
  try{
    const r=await fetch('/api/owner/config');const d=await r.json();
    document.getElementById('t-daily').value=d.daily_checkin.reward;
    document.getElementById('t-share').value=d.share_app.reward;
    document.getElementById('t-invite').value=d.invite_friend.reward;
    linksState=d.links||[];renderLinks();
  }catch(e){}
}
function renderLinks(){
  const el=document.getElementById('links-container');
  if(!linksState.length){el.innerHTML='<p style="color:var(--muted);font-size:.8rem;padding:10px">لا روابط بعد</p>';return}
  el.innerHTML=linksState.map((lk,i)=>`<div class="link-row">
    <input class="form-input" value="${lk.icon||''}" onchange="linksState[${i}].icon=this.value" placeholder="إيموجي">
    <input class="form-input" value="${lk.title||''}" onchange="linksState[${i}].title=this.value" placeholder="العنوان">
    <input class="form-input" value="${lk.url||''}" onchange="linksState[${i}].url=this.value" placeholder="https://">
    <input type="number" class="form-input" value="${lk.reward||10}" onchange="linksState[${i}].reward=+this.value">
    <button class="btn btn-ban" onclick="linksState.splice(${i},1);renderLinks()">🗑️</button>
  </div>`).join('');
}
function addLink(){linksState.push({id:'l'+Date.now(),title:'',url:'',reward:10,icon:'🔗'});renderLinks()}
async function saveConfig(){
  const f=new FormData();
  f.append('daily_reward',document.getElementById('t-daily').value);
  f.append('share_reward',document.getElementById('t-share').value);
  f.append('invite_reward',document.getElementById('t-invite').value);
  f.append('links_json',JSON.stringify(linksState));
  const r=await fetch('/api/owner/config',{method:'POST',body:f});
  if(r.ok)toast('✅ تم الحفظ');else toast('❌ فشل','error');
}
async function changePass(){
  const o=document.getElementById('old-pass').value;const n=document.getElementById('new-pass').value;
  if(!o||!n)return toast('املأ الحقول','error');
  const f=new FormData();f.append('old_password',o);f.append('new_password',n);
  const r=await fetch('/api/owner/change-password',{method:'POST',body:f});
  if(r.ok){toast('✅ تم');document.getElementById('old-pass').value='';document.getElementById('new-pass').value=''}else{const d=await r.json().catch(()=>({}));toast('❌ '+(d.detail||'فشل'),'error')}
}
loadStats();
</script></body></html>"""


# ═══════════════════════════════════════════════════════════════════
#  ASSETS
# ═══════════════════════════════════════════════════════════════════
SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#00e0ff"/><stop offset="0.5" stop-color="#7c5cff"/><stop offset="1" stop-color="#ff2d95"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="#05070f"/><circle cx="256" cy="256" r="140" fill="none" stroke="url(#g)" stroke-width="20"/><path d="M200 320 L256 200 L312 320 Z" fill="url(#g)"/><circle cx="256" cy="180" r="22" fill="url(#g)"/></svg>"""

SW_JS = """self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;if(e.request.url.includes('/api/'))return;if(e.request.url.endsWith('/ping'))return;e.respondWith(caches.open('lux-v8').then(c=>fetch(e.request).then(r=>{if(r.ok&&r.type==='basic')c.put(e.request,r.clone());return r}).catch(()=>c.match(e.request))))});"""


# ═══════════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"👑 Lux Studio VIP v8 → port {PORT}", flush=True)
    print(f"📦 DSS: {DSS_BASE_URL}", flush=True)
    print(f"🔐 Owner: /owner (password: {OWNER_PASSWORD})", flush=True)
    print(f"⚡ Ping: /ping (never creates users)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
