"""
╔══════════════════════════════════════════════════════════════════════╗
║  👑 LUX IMAGE STUDIO VIP — Premium Edition                           ║
║  ──────────────────────────────────────────────────────────────────  ║
║  ملف واحد | مفتاح DSS مضمّن | PWA | 10 أدوات صور                    ║
║  لوحة مدير محمية بكلمة مرور (xeon) | إدارة مستخدمين ومفاتيح         ║
║  دارك/لايت | عربي/إنجليزي | إحصائيات حية | سجل عمليات              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════
#  [1] Imports
# ═══════════════════════════════════════════════════════════════════
import os, sys, io, time, secrets, hashlib, logging, asyncio, contextlib
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict, Counter

import httpx
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from fastapi import FastAPI, File, UploadFile, Header, HTTPException, Request, Form, Cookie
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ═══════════════════════════════════════════════════════════════════
#  [2] Configuration
# ═══════════════════════════════════════════════════════════════════
DSS_BASE_URL = "https://kali-imvx.onrender.com"
DSS_API_KEY  = "dss_SSiPIYB4EpWwVmfygQ1LTF2KymOjd-qxAQ4U3Q"

ADMIN_PASSWORD = "xeon"           # ← كلمة مرور لوحة المدير
ADMIN_SESSION_HOURS = 24

FREE_CREDITS   = 25
MAX_FILE_MB    = 15
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
PORT           = int(os.getenv("PORT", "8000"))
RATE_WINDOW    = 60
RATE_LIMIT     = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout, force=True,
)
log = logging.getLogger("lux")

# ═══════════════════════════════════════════════════════════════════
#  [3] rembg lazy loader
# ═══════════════════════════════════════════════════════════════════
REMBG_MODEL = "u2netp"
_rembg_remove = None
_rembg_ready = False
_rembg_lock = asyncio.Lock()
_rembg_event = asyncio.Event()


def _load_rembg_sync():
    global _rembg_remove, _rembg_ready
    try:
        log.info("📦 تحميل rembg...")
        from rembg import remove as rr, new_session
        _rembg_remove = rr
        try:
            new_session(REMBG_MODEL)
            log.info(f"✅ موديل '{REMBG_MODEL}' جاهز")
        except Exception as e:
            log.warning(f"⚠️ preload: {e}")
    except Exception as e:
        log.error(f"❌ rembg: {e}")
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
#  [4] DSS Client
# ═══════════════════════════════════════════════════════════════════
class DSSError(Exception): pass


class DSS:
    def __init__(self):
        self.base = DSS_BASE_URL.rstrip("/")
        self.headers = {
            "X-API-Key": DSS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _req(self, method: str, path: str, retries: int = 2, **kw):
        url = f"{self.base}{path}"
        last = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    r = await c.request(method, url, headers=self.headers, **kw)
                    r.raise_for_status()
                    return r.json() if r.content else {}
            except httpx.HTTPStatusError as e:
                body = e.response.text[:200] if e.response is not None else ""
                last = DSSError(f"{method} {path} → {e.response.status_code}: {body}")
                if 400 <= e.response.status_code < 500 and e.response.status_code != 404:
                    raise last
            except httpx.RequestError as e:
                last = DSSError(f"network {method} {path}: {e}")
            if attempt < retries:
                await asyncio.sleep(0.6)
        raise last or DSSError("unknown")

    async def list(self, collection: str) -> list:
        try:
            res = await self._req("GET", f"/api/db/{collection}")
        except DSSError as e:
            if "404" in str(e): return []
            raise
        if isinstance(res, list): return res
        if isinstance(res, dict):
            for k in ("items","data","results","records","posts","rows","documents","docs","list"):
                if isinstance(res.get(k), list): return res[k]
            if "id" in res or "_id" in res: return [res]
        return []

    async def create(self, collection: str, data: dict) -> dict:
        res = await self._req("POST", f"/api/db/{collection}", json=data)
        if isinstance(res, dict):
            for key in ("id","_id","uuid","key","ID"):
                if key in res: return {**data, **res}
            if isinstance(res.get("data"), dict): return {**data, **res["data"]}
        return {**data, "_raw": res}

    async def update(self, collection: str, doc_id, data: dict) -> dict:
        for method in ("PATCH", "PUT"):
            try:
                return await self._req(method, f"/api/db/{collection}/{doc_id}", json=data)
            except DSSError: continue
        try:
            return await self._req("POST", f"/api/db/{collection}", json={**data, "id": doc_id})
        except DSSError as e:
            log.warning(f"update fallback: {e}")
            return {}

    async def delete(self, collection: str, doc_id) -> bool:
        try:
            await self._req("DELETE", f"/api/db/{collection}/{doc_id}")
            return True
        except DSSError:
            return False

    @staticmethod
    def doc_id(doc: dict):
        if not isinstance(doc, dict): return None
        for k in ("id","_id","uuid","key","ID"):
            if k in doc and doc[k] is not None: return doc[k]
        return None


dss = DSS()


# ═══════════════════════════════════════════════════════════════════
#  [5] Rate limiter + Admin sessions
# ═══════════════════════════════════════════════════════════════════
_rate_buckets: dict = defaultdict(list)
_admin_sessions: dict = {}  # token -> expires_at (timestamp)


def rate_check(key: str) -> bool:
    now = time.time()
    bucket = _rate_buckets[key]
    _rate_buckets[key] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(_rate_buckets[key]) >= RATE_LIMIT:
        return False
    _rate_buckets[key].append(now)
    return True


def new_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = time.time() + ADMIN_SESSION_HOURS * 3600
    # cleanup expired
    now = time.time()
    for t in list(_admin_sessions.keys()):
        if _admin_sessions[t] < now:
            _admin_sessions.pop(t, None)
    return token


def check_admin_session(token: Optional[str]) -> bool:
    if not token: return False
    exp = _admin_sessions.get(token)
    if not exp: return False
    if exp < time.time():
        _admin_sessions.pop(token, None)
        return False
    return True


def require_admin(lux_admin: Optional[str] = Cookie(None)):
    if not check_admin_session(lux_admin):
        raise HTTPException(401, "جلسة مدير مطلوبة")
    return True


# ═══════════════════════════════════════════════════════════════════
#  [6] Users & Auth
# ═══════════════════════════════════════════════════════════════════
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def get_user_by_id(uid: str) -> Optional[dict]:
    for u in await dss.list("users"):
        if str(u.get("id")) == str(uid) or str(u.get("_id")) == str(uid):
            return u
    return None


async def find_api_key(plain: str) -> Optional[dict]:
    target = sha256(plain.strip())
    try:
        keys = await dss.list("api_keys")
    except DSSError as e:
        log.error(f"find_api_key: {e}")
        return None
    for k in keys:
        if k.get("hash") == target and k.get("active", True):
            return k
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
    except DSSError: pass
    return count


async def create_free_key(ip: str) -> dict:
    user_id = secrets.token_urlsafe(12)
    await dss.create("users", {
        "id": user_id, "ip": ip, "credits": FREE_CREDITS,
        "usage_count": 0, "is_banned": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    plain = f"lux_{secrets.token_urlsafe(32)}"
    prefix = plain[:12]
    await dss.create("api_keys", {
        "id": secrets.token_urlsafe(12),
        "user_id": user_id,
        "hash": sha256(plain), "prefix": prefix, "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"api_key": plain, "prefix": prefix, "credits": FREE_CREDITS, "user_id": user_id}


async def deduct_credit(user_id: str) -> bool:
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
        })
        return True
    except: return False


async def add_credits(user_id: str, amount: int) -> bool:
    user = await get_user_by_id(user_id)
    if not user: return False
    did = DSS.doc_id(user)
    if did is None: return False
    try:
        await dss.update("users", did, {"credits": int(user.get("credits", 0)) + amount})
        return True
    except: return False


async def log_usage(user_id: str, action: str, status: str, duration_ms: int, size_bytes: int = 0):
    try:
        await dss.create("usage_logs", {
            "id": secrets.token_urlsafe(12),
            "user_id": user_id, "action": action, "status": status,
            "duration_ms": duration_ms, "size_bytes": size_bytes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.warning(f"log_usage: {e}")


async def authenticate(x_api_key: Optional[str]) -> dict:
    if not x_api_key:
        raise HTTPException(401, "مفتاح API مطلوب في الهيدر X-API-Key")
    rec = await find_api_key(x_api_key.strip())
    if not rec:
        raise HTTPException(401, "مفتاح API غير صالح أو ملغى")
    user = await get_user_by_id(rec.get("user_id"))
    if user and user.get("is_banned"):
        raise HTTPException(403, "الحساب محظور")
    return rec


# ═══════════════════════════════════════════════════════════════════
#  [7] Image processing (10 tools)
# ═══════════════════════════════════════════════════════════════════
def _open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as e:
        raise ValueError(f"ملف صورة غير صالح: {e}")


def _save(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    out = io.BytesIO()
    img.save(out, format=fmt, **kw)
    return out.getvalue()


def tool_remove_bg(data: bytes) -> bytes:
    if _rembg_remove is None:
        raise RuntimeError("الموديل قيد التحميل — أعد المحاولة بعد 30ث")
    try:
        return _rembg_remove(data, model_name=REMBG_MODEL)
    except Exception:
        return _rembg_remove(data)


def tool_enhance(data: bytes) -> tuple:
    img = _open_image(data)
    has_alpha = img.mode in ("RGBA","LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if has_alpha else "RGB")
    w, h = img.size
    nw, nh = w * 2, h * 2
    if max(nw, nh) > 2048:
        r = 2048 / max(nw, nh)
        nw, nh = int(nw * r), int(nh * r)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    img = ImageEnhance.Sharpness(img).enhance(1.4)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.18)
    img = ImageEnhance.Brightness(img).enhance(1.02)
    if has_alpha: return _save(img, "PNG", optimize=True), "png"
    return _save(img, "JPEG", quality=92, optimize=True), "jpg"


def tool_compress(data: bytes, quality: int = 75) -> bytes:
    img = _open_image(data)
    if img.mode not in ("RGB","RGBA"):
        img = img.convert("RGBA" if img.mode in ("P","LA") else "RGB")
    q = max(10, min(95, quality))
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True)
    return _save(img, "JPEG", quality=q, optimize=True)


def tool_convert(data: bytes, target: str) -> tuple:
    img = _open_image(data)
    target = target.lower()
    if target not in ("jpg","jpeg","png","webp"): raise ValueError(f"صيغة غير مدعومة: {target}")
    if target in ("jpg","jpeg"):
        if img.mode != "RGB": img = img.convert("RGB")
        return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"
    elif target == "png":
        if img.mode not in ("RGBA","RGB"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        return _save(img, "PNG", optimize=True), "image/png", "png"
    else:
        if img.mode not in ("RGB","RGBA"): img = img.convert("RGB")
        return _save(img, "WEBP", quality=90, method=6), "image/webp", "webp"


def tool_grayscale(data: bytes) -> bytes:
    img = _open_image(data)
    if img.mode in ("RGBA","LA"):
        alpha = img.split()[-1]
        gray = ImageOps.grayscale(img.convert("RGB")).convert("RGBA")
        gray.putalpha(alpha)
        img = gray
    else:
        img = ImageOps.grayscale(img.convert("RGB"))
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True)
    return _save(img, "JPEG", quality=92, optimize=True)


def tool_rotate(data: bytes, angle: int = 90) -> bytes:
    img = _open_image(data)
    angle = angle % 360
    img = img.rotate(-angle, expand=True, fillcolor=(0,0,0,0) if img.mode == "RGBA" else None)
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True)
    return _save(img, "JPEG", quality=92, optimize=True)


def tool_flip(data: bytes, direction: str = "h") -> bytes:
    img = _open_image(data)
    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT if direction == "h" else Image.Transpose.FLIP_TOP_BOTTOM)
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True)
    return _save(img, "JPEG", quality=92, optimize=True)


def tool_blur(data: bytes, radius: int = 5) -> bytes:
    img = _open_image(data)
    r = max(1, min(30, radius))
    img = img.filter(ImageFilter.GaussianBlur(radius=r))
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True)
    return _save(img, "JPEG", quality=92, optimize=True)


def tool_sepia(data: bytes) -> bytes:
    img = _open_image(data).convert("RGB")
    pixels = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            tr = int(0.393*r + 0.769*g + 0.189*b)
            tg = int(0.349*r + 0.686*g + 0.168*b)
            tb = int(0.272*r + 0.534*g + 0.131*b)
            pixels[x, y] = (min(255,tr), min(255,tg), min(255,tb))
    return _save(img, "JPEG", quality=92, optimize=True)


def tool_invert(data: bytes) -> bytes:
    img = _open_image(data)
    if img.mode == "RGBA":
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        inv = ImageOps.invert(rgb)
        r2, g2, b2 = inv.split()
        img = Image.merge("RGBA", (r2, g2, b2, a))
        return _save(img, "PNG", optimize=True)
    img = img.convert("RGB")
    return _save(ImageOps.invert(img), "JPEG", quality=92, optimize=True)


TOOLS = {
    "remove-bg": {"fn": tool_remove_bg, "label": "إزالة الخلفية"},
    "enhance":   {"fn": tool_enhance,   "label": "تحسين"},
    "compress":  {"fn": tool_compress,  "label": "ضغط"},
    "convert":   {"fn": tool_convert,   "label": "تحويل"},
    "grayscale": {"fn": tool_grayscale, "label": "رمادي"},
    "rotate":    {"fn": tool_rotate,    "label": "تدوير"},
    "flip":      {"fn": tool_flip,      "label": "قلب"},
    "blur":      {"fn": tool_blur,      "label": "بلور"},
    "sepia":     {"fn": tool_sepia,     "label": "سيبيا"},
    "invert":    {"fn": tool_invert,    "label": "انعكاس"},
}


# ═══════════════════════════════════════════════════════════════════
#  [8] Lifespan
# ═══════════════════════════════════════════════════════════════════
async def _keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url: return
    while True:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                await c.get(f"{url}/ping")
        except: pass


@contextlib.asynccontextmanager
async def lifespan(app):
    bg = asyncio.create_task(_ensure_rembg_loaded())
    ka = asyncio.create_task(_keep_alive())
    log.info("🚀 Lux Studio VIP جاهز")
    try:
        yield
    finally:
        for t in (bg, ka): t.cancel()
        for t in (bg, ka):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# ═══════════════════════════════════════════════════════════════════
#  [9] FastAPI App
# ═══════════════════════════════════════════════════════════════════
app = FastAPI(title="Lux Image Studio VIP", version="5.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
async def index(): return HTML_PAGE


@app.get("/ping")
async def ping(): return {"status": "alive", "ts": time.time(), "rembg_ready": _rembg_ready}


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Lux Studio VIP", "short_name": "Lux VIP",
        "start_url": "/", "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#05070f", "theme_color": "#00e0ff",
        "description": "استوديو احترافي — 10 أدوات صور",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
    })


@app.get("/icon.svg")
async def icon(): return Response(content=SVG_ICON, media_type="image/svg+xml")


@app.get("/sw.js")
async def sw(): return Response(content=SW_JS, media_type="application/javascript")


@app.get("/api/health")
async def health():
    result = {"status": "ok", "rembg_ready": _rembg_ready, "version": "5.0.0-vip"}
    try:
        users = await dss.list("users")
        result["dss"] = "connected"; result["dss_users"] = len(users)
    except Exception as e:
        result["dss"] = f"error: {e}"; result["status"] = "degraded"
    return result


# ─── Keys / Me / History ───
@app.post("/api/keys/new")
async def api_new_key(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not rate_check(f"key:{ip}"):
        raise HTTPException(429, "محاولات كثيرة")
    try:
        if await ip_key_count(ip) >= 3:
            raise HTTPException(429, "3 مفاتيح كحد أقصى يوميًا")
        return {**await create_free_key(ip), "message": "احفظ هذا المفتاح الآن"}
    except HTTPException: raise
    except DSSError as e: raise HTTPException(503, f"DB: {e}")


@app.get("/api/me")
async def api_me(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec.get("user_id"))
    if not user: raise HTTPException(404, "المستخدم غير موجود")
    try:
        logs = [l for l in await dss.list("usage_logs") if l.get("user_id") == rec.get("user_id")]
    except: logs = []
    return {
        "prefix": rec.get("prefix"),
        "credits": int(user.get("credits", 0)),
        "total_operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
        "is_banned": bool(user.get("is_banned")),
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


# ─── Image Tools ───
async def _process_request(tool_name: str, file: UploadFile, rec: dict):
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1:
        raise HTTPException(402, "رصيدك انتهى")
    if not rate_check(f"user:{rec['user_id']}"):
        raise HTTPException(429, "محاولات كثيرة")

    data = await file.read()
    if len(data) == 0: raise HTTPException(400, "ملف فارغ")
    if len(data) > MAX_FILE_BYTES: raise HTTPException(413, f"يتجاوز {MAX_FILE_MB}MB")

    if tool_name == "remove-bg" and not _rembg_ready:
        try: await asyncio.wait_for(_rembg_event.wait(), timeout=120.0)
        except asyncio.TimeoutError: raise HTTPException(503, "الموديل قيد التحميل")

    t0 = time.time()
    tool = TOOLS[tool_name]
    try:
        result = await asyncio.to_thread(tool["fn"], data)
    except ValueError as e:
        await log_usage(rec["user_id"], tool_name, "error", 0)
        raise HTTPException(400, str(e))
    except Exception as e:
        await log_usage(rec["user_id"], tool_name, "error", 0)
        raise HTTPException(500, str(e))

    media_type = "image/png"
    ext = "png"
    if isinstance(result, tuple):
        if len(result) == 2: result, ext = result; media_type = f"image/{ext}"
        else: result, media_type, ext = result

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], tool_name, "success",
                    int((time.time() - t0) * 1000), len(result))
    return Response(
        content=result, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="lux_{tool_name}.{ext}"'},
    )


@app.post("/api/remove-bg")
async def api_remove_bg(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    return await _process_request("remove-bg", file, await authenticate(x_api_key))


@app.post("/api/enhance")
async def api_enhance(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    return await _process_request("enhance", file, await authenticate(x_api_key))


@app.post("/api/grayscale")
async def api_grayscale(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    return await _process_request("grayscale", file, await authenticate(x_api_key))


@app.post("/api/sepia")
async def api_sepia(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    return await _process_request("sepia", file, await authenticate(x_api_key))


@app.post("/api/invert")
async def api_invert(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    return await _process_request("invert", file, await authenticate(x_api_key))


@app.post("/api/compress")
async def api_compress(file: UploadFile = File(...), quality: int = 75,
                      x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "رصيدك انتهى")
    data = await file.read()
    if len(data) > MAX_FILE_BYTES: raise HTTPException(413, "كبير جدًا")
    t0 = time.time()
    try:
        result = await asyncio.to_thread(tool_compress, data, quality)
        await deduct_credit(rec["user_id"])
        await log_usage(rec["user_id"], "compress", "success", int((time.time()-t0)*1000), len(result))
        return Response(content=result, media_type="image/jpeg",
                        headers={"Content-Disposition": 'attachment; filename="compressed.jpg"'})
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/api/convert")
async def api_convert(file: UploadFile = File(...), target: str = "png",
                     x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "رصيدك انتهى")
    data = await file.read()
    t0 = time.time()
    try:
        result, mime, ext = await asyncio.to_thread(tool_convert, data, target)
        await deduct_credit(rec["user_id"])
        await log_usage(rec["user_id"], "convert", "success", int((time.time()-t0)*1000), len(result))
        return Response(content=result, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="converted.{ext}"'})
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/api/rotate")
async def api_rotate(file: UploadFile = File(...), angle: int = 90,
                    x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "رصيدك انتهى")
    data = await file.read()
    try:
        result = await asyncio.to_thread(tool_rotate, data, angle)
        await deduct_credit(rec["user_id"])
        await log_usage(rec["user_id"], "rotate", "success", 0, len(result))
        return Response(content=result, media_type="image/png",
                        headers={"Content-Disposition": 'attachment; filename="rotated.png"'})
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/api/flip")
async def api_flip(file: UploadFile = File(...), direction: str = "h",
                  x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "رصيدك انتهى")
    data = await file.read()
    try:
        result = await asyncio.to_thread(tool_flip, data, direction)
        await deduct_credit(rec["user_id"])
        await log_usage(rec["user_id"], "flip", "success", 0, len(result))
        return Response(content=result, media_type="image/png",
                        headers={"Content-Disposition": 'attachment; filename="flipped.png"'})
    except Exception as e: raise HTTPException(500, str(e))


@app.post("/api/blur")
async def api_blur(file: UploadFile = File(...), radius: int = 5,
                  x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "رصيدك انتهى")
    data = await file.read()
    try:
        result = await asyncio.to_thread(tool_blur, data, radius)
        await deduct_credit(rec["user_id"])
        await log_usage(rec["user_id"], "blur", "success", 0, len(result))
        return Response(content=result, media_type="image/png",
                        headers={"Content-Disposition": 'attachment; filename="blurred.png"'})
    except Exception as e: raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════
#  [10] ADMIN — Login + Dashboard + APIs
# ═══════════════════════════════════════════════════════════════════
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(lux_admin: Optional[str] = Cookie(None)):
    if check_admin_session(lux_admin):
        return ADMIN_DASHBOARD_HTML
    return ADMIN_LOGIN_HTML


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        return RedirectResponse("/admin?error=1", status_code=303)
    token = new_admin_session()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "lux_admin", token,
        max_age=ADMIN_SESSION_HOURS * 3600,
        httponly=True, samesite="lax",
    )
    return response


@app.post("/admin/logout")
async def admin_logout(lux_admin: Optional[str] = Cookie(None)):
    if lux_admin:
        _admin_sessions.pop(lux_admin, None)
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("lux_admin")
    return response


# ─── Admin APIs (protected) ───
@app.get("/api/admin/stats")
async def admin_stats(lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    try:
        users = await dss.list("users")
        keys = await dss.list("api_keys")
        logs = await dss.list("usage_logs")
    except DSSError as e:
        raise HTTPException(503, str(e))

    daily = defaultdict(int)
    for l in logs:
        try:
            day = str(l.get("created_at",""))[:10]
            if day: daily[day] += 1
        except: pass
    by_action = Counter(l.get("action","?") for l in logs)
    return {
        "users": len(users),
        "keys_total": len(keys),
        "keys_active": sum(1 for k in keys if k.get("active")),
        "operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
        "banned": sum(1 for u in users if u.get("is_banned")),
        "total_credits": sum(int(u.get("credits", 0)) for u in users),
        "daily": dict(sorted(daily.items())[-14:]),
        "by_action": dict(by_action.most_common(10)),
    }


@app.get("/api/admin/users")
async def admin_users(lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    try:
        users = await dss.list("users")
        keys = await dss.list("api_keys")
    except DSSError as e:
        raise HTTPException(503, str(e))
    # attach key prefix to each user
    key_by_user = {}
    for k in keys:
        uid = k.get("user_id")
        if uid and (uid not in key_by_user or not key_by_user[uid].get("active")):
            key_by_user[uid] = k
    items = []
    for u in users:
        uid = str(u.get("id") or u.get("_id") or "")
        k = key_by_user.get(uid) or {}
        items.append({
            "id": uid,
            "ip": u.get("ip", "-"),
            "credits": int(u.get("credits", 0)),
            "usage_count": int(u.get("usage_count", 0)),
            "is_banned": bool(u.get("is_banned")),
            "created_at": u.get("created_at", ""),
            "prefix": k.get("prefix", "-"),
            "key_active": bool(k.get("active", False)),
        })
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"items": items[:200], "total": len(items)}


@app.get("/api/admin/keys")
async def admin_keys(lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    try:
        keys = await dss.list("api_keys")
    except DSSError as e:
        raise HTTPException(503, str(e))
    items = [{
        "id": DSS.doc_id(k) or "",
        "user_id": k.get("user_id", ""),
        "prefix": k.get("prefix", ""),
        "active": bool(k.get("active", False)),
        "created_at": k.get("created_at", ""),
    } for k in keys]
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"items": items[:300], "total": len(items)}


@app.get("/api/admin/logs")
async def admin_logs(lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    try:
        logs = await dss.list("usage_logs")
    except DSSError as e:
        raise HTTPException(503, str(e))
    logs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"items": logs[:100]}


@app.post("/api/admin/user/credits")
async def admin_user_credits(user_id: str = Form(...), amount: int = Form(...),
                            lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    if amount == 0: raise HTTPException(400, "المقدار صفر")
    ok = await add_credits(user_id, amount)
    if not ok: raise HTTPException(404, "المستخدم غير موجود")
    return {"ok": True, "user_id": user_id, "amount": amount}


@app.post("/api/admin/user/ban")
async def admin_user_ban(user_id: str = Form(...), ban: bool = Form(True),
                        lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    user = await get_user_by_id(user_id)
    if not user: raise HTTPException(404, "المستخدم غير موجود")
    did = DSS.doc_id(user)
    if did is None: raise HTTPException(400, "معرف غير صالح")
    await dss.update("users", did, {"is_banned": ban})
    return {"ok": True, "user_id": user_id, "is_banned": ban}


@app.post("/api/admin/user/delete")
async def admin_user_delete(user_id: str = Form(...),
                           lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    user = await get_user_by_id(user_id)
    if not user: raise HTTPException(404, "المستخدم غير موجود")
    did = DSS.doc_id(user)
    if did is None: raise HTTPException(400, "معرف غير صالح")
    # revoke all keys for this user
    try:
        keys = await dss.list("api_keys")
        for k in keys:
            if k.get("user_id") == user_id:
                kid = DSS.doc_id(k)
                if kid is not None:
                    await dss.update("api_keys", kid, {"active": False})
    except: pass
    await dss.delete("users", did)
    return {"ok": True}


@app.post("/api/admin/key/revoke")
async def admin_key_revoke(key_id: str = Form(...),
                          lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    await dss.update("api_keys", key_id, {"active": False})
    return {"ok": True, "key_id": key_id}


@app.post("/api/admin/key/delete")
async def admin_key_delete(key_id: str = Form(...),
                          lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    ok = await dss.delete("api_keys", key_id)
    if not ok: raise HTTPException(404, "المفتاح غير موجود")
    return {"ok": True}


@app.post("/api/admin/change-password")
async def admin_change_password(old_password: str = Form(...), new_password: str = Form(...),
                                lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    global ADMIN_PASSWORD
    if old_password != ADMIN_PASSWORD:
        raise HTTPException(401, "كلمة المرور القديمة خاطئة")
    if len(new_password) < 4:
        raise HTTPException(400, "كلمة المرور قصيرة جدًا (4 أحرف على الأقل)")
    ADMIN_PASSWORD = new_password
    log.info("🔑 كلمة مرور المدير تغيّرت")
    return {"ok": True, "message": "تم تغيير كلمة المرور بنجاح"}


# ═══════════════════════════════════════════════════════════════════
#  [11] ADMIN LOGIN HTML
# ═══════════════════════════════════════════════════════════════════
ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>دخول المدير — Lux VIP</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:#05070f;color:#e8ecf4;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background-image:radial-gradient(circle at 20% 20%,rgba(0,224,255,.12),transparent 40%),radial-gradient(circle at 80% 30%,rgba(124,92,255,.14),transparent 40%),radial-gradient(circle at 50% 90%,rgba(255,45,149,.09),transparent 40%)}
.card{background:rgba(255,255,255,.04);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:48px 40px;max-width:420px;width:100%;box-shadow:0 30px 80px rgba(0,0,0,.6);text-align:center}
.icon{font-size:3.5rem;margin-bottom:16px}
h1{font-size:1.6rem;margin-bottom:8px;background:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#8a94a8;font-size:.9rem;margin-bottom:32px}
label{display:block;text-align:right;font-weight:600;margin-bottom:8px;font-size:.85rem;color:#8a94a8}
input[type=password]{width:100%;padding:14px 16px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:#05070f;color:#e8ecf4;font-family:inherit;font-size:1rem;margin-bottom:20px;transition:border-color .2s}
input[type=password]:focus{outline:none;border-color:#00e0ff}
button{width:100%;padding:14px;border:none;border-radius:12px;background:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95);color:#05070f;font-family:inherit;font-size:1rem;font-weight:800;cursor:pointer;transition:transform .2s;box-shadow:0 8px 30px rgba(124,92,255,.4)}
button:hover{transform:translateY(-2px)}
.error{color:#ff4d6d;font-size:.85rem;margin-bottom:16px;padding:10px;background:rgba(255,77,109,.1);border:1px solid rgba(255,77,109,.3);border-radius:8px}
.back{display:inline-block;margin-top:24px;color:#8a94a8;text-decoration:none;font-size:.85rem;transition:color .2s}
.back:hover{color:#00e0ff}
</style>
</head>
<body>
<div class="card">
<div class="icon">👑</div>
<h1>لوحة المدير</h1>
<p class="sub">Lux Studio VIP — Admin Panel</p>
<form method="post" action="/admin/login">
<label for="password">كلمة المرور</label>
<input type="password" id="password" name="password" required autofocus placeholder="••••••••">
<button type="submit">🔓 دخول</button>
</form>
<a href="/" class="back">← العودة للتطبيق</a>
</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
#  [12] ADMIN DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════
ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>لوحة المدير — Lux VIP</title>
<style>
:root{--bg:#05070f;--bg2:#0a0e1a;--surface:rgba(255,255,255,.04);--surface2:rgba(255,255,255,.08);--border:rgba(255,255,255,.08);--text:#e8ecf4;--muted:#8a94a8;--cyan:#00e0ff;--purple:#7c5cff;--pink:#ff2d95;--success:#00ffa3;--danger:#ff4d6d;--warn:#ffb020;--grad:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6}
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(circle at 15% 20%,rgba(0,224,255,.10),transparent 35%),radial-gradient(circle at 85% 15%,rgba(124,92,255,.12),transparent 35%)}
header{background:rgba(5,7,15,.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.logo{font-weight:900;font-size:1.3rem}
.logo span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.logout{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 16px;border-radius:8px;cursor:pointer;font-family:inherit;font-weight:600;font-size:.85rem}
.logout:hover{background:rgba(255,77,109,.2);border-color:var(--danger);color:var(--danger)}
main{max-width:1400px;margin:0 auto;padding:24px}
h1{font-size:1.6rem;margin-bottom:24px}
h2{font-size:1.2rem;margin-bottom:16px;color:var(--cyan)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:28px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;text-align:center}
.stat .num{font-size:2rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:monospace}
.stat .lbl{color:var(--muted);font-size:.8rem;margin-top:4px}
.tabs{display:flex;gap:8px;margin-bottom:20px;border-bottom:1px solid var(--border);overflow-x:auto;padding-bottom:0}
.tab{padding:12px 20px;background:transparent;border:none;color:var(--muted);cursor:pointer;font-family:inherit;font-weight:700;font-size:.9rem;border-bottom:2px solid transparent;transition:all .2s;white-space:nowrap}
.tab.active{color:var(--cyan);border-bottom-color:var(--cyan)}
.panel{display:none}
.panel.active{display:block}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th{text-align:right;padding:12px 8px;color:var(--muted);font-weight:600;font-size:.75rem;text-transform:uppercase;border-bottom:1px solid var(--border)}
td{padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle}
tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:monospace;font-size:.78rem;color:var(--cyan)}
.btn{padding:6px 12px;border:none;border-radius:6px;font-family:inherit;font-size:.75rem;font-weight:700;cursor:pointer;transition:all .2s;margin:2px}
.btn-add{background:rgba(0,255,163,.2);color:var(--success);border:1px solid rgba(0,255,163,.3)}
.btn-add:hover{background:rgba(0,255,163,.3)}
.btn-ban{background:rgba(255,176,32,.2);color:var(--warn);border:1px solid rgba(255,176,32,.3)}
.btn-ban:hover{background:rgba(255,176,32,.3)}
.btn-del{background:rgba(255,77,109,.2);color:var(--danger);border:1px solid rgba(255,77,109,.3)}
.btn-del:hover{background:rgba(255,77,109,.3)}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.7rem;font-weight:700}
.badge-ok{background:rgba(0,255,163,.15);color:var(--success);border:1px solid rgba(0,255,163,.3)}
.badge-ban{background:rgba(255,77,109,.15);color:var(--danger);border:1px solid rgba(255,77,109,.3)}
.badge-inactive{background:rgba(255,255,255,.08);color:var(--muted);border:1px solid var(--border)}
.empty{text-align:center;padding:40px;color:var(--muted)}
.search{width:100%;padding:10px 14px;border-radius:10px;border:1px solid var(--border);background:var(--bg2);color:var(--text);font-family:inherit;font-size:.9rem;margin-bottom:16px}
.search:focus{outline:none;border-color:var(--cyan)}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(150%);background:var(--bg2);border:1px solid var(--border);padding:12px 22px;border-radius:12px;z-index:999;font-weight:600;font-size:.9rem;transition:transform .3s;max-width:90%}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:28px;max-width:400px;width:100%}
.modal h3{margin-bottom:16px;font-size:1.1rem}
.modal input{width:100%;padding:12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-family:inherit;margin-bottom:16px}
.modal .actions{display:flex;gap:8px}
.modal button{flex:1;padding:12px;border:none;border-radius:8px;font-family:inherit;font-weight:700;cursor:pointer}
.modal .cancel{background:var(--surface2);color:var(--text)}
.modal .confirm{background:var(--grad);color:var(--bg)}
@media(max-width:600px){table{font-size:.75rem}th,td{padding:6px 4px}.btn{font-size:.65rem;padding:4px 8px}}
</style>
</head>
<body>
<header>
<div class="logo">👑 Lux <span>VIP</span> Admin</div>
<form method="post" action="/admin/logout" style="margin:0">
<button type="submit" class="logout">🚪 خروج</button>
</form>
</header>
<main>
<h1>📊 لوحة التحكم</h1>
<div class="stats" id="stats">
<div class="stat"><div class="num" id="s-users">-</div><div class="lbl">مستخدم</div></div>
<div class="stat"><div class="num" id="s-keys">-</div><div class="lbl">مفتاح نشط</div></div>
<div class="stat"><div class="num" id="s-ops">-</div><div class="lbl">عملية</div></div>
<div class="stat"><div class="num" id="s-success">-</div><div class="lbl">ناجحة</div></div>
<div class="stat"><div class="num" id="s-banned">-</div><div class="lbl">محظور</div></div>
<div class="stat"><div class="num" id="s-credits">-</div><div class="lbl">إجمالي نقاط</div></div>
</div>

<div class="tabs">
<button class="tab active" onclick="switchTab('users')">👥 المستخدمون</button>
<button class="tab" onclick="switchTab('keys')">🔑 المفاتيح</button>
<button class="tab" onclick="switchTab('logs')">📜 العمليات</button>
<button class="tab" onclick="switchTab('settings')">⚙️ الإعدادات</button>
</div>

<div class="panel active" id="panel-users">
<input type="text" class="search" id="search-users" placeholder="🔍 بحث بالمعرف أو IP..." oninput="renderUsers()">
<div class="card"><div id="users-table"></div></div>
</div>

<div class="panel" id="panel-keys">
<input type="text" class="search" id="search-keys" placeholder="🔍 بحث بالبادئة أو معرف المستخدم..." oninput="renderKeys()">
<div class="card"><div id="keys-table"></div></div>
</div>

<div class="panel" id="panel-logs">
<div class="card"><div id="logs-table"></div></div>
</div>

<div class="panel" id="panel-settings">
<div class="card" style="max-width:500px">
<h2>🔑 تغيير كلمة المرور</h2>
<form onsubmit="changePassword(event)">
<input type="password" id="old-pass" placeholder="كلمة المرور الحالية" class="search" required>
<input type="password" id="new-pass" placeholder="كلمة المرور الجديدة (4+ أحرف)" class="search" required minlength="4">
<button type="submit" class="btn btn-add" style="padding:12px 24px;font-size:.9rem">💾 حفظ كلمة المرور</button>
</form>
</div>
</div>
</main>

<div class="modal-overlay" id="modal">
<div class="modal">
<h3 id="modal-title">تأكيد</h3>
<p id="modal-desc" style="color:var(--muted);margin-bottom:16px;font-size:.9rem"></p>
<input type="number" id="modal-amount" placeholder="المقدار" style="display:none">
<div class="actions">
<button class="cancel" onclick="closeModal()">إلغاء</button>
<button class="confirm" id="modal-confirm" onclick="confirmModal()">تأكيد</button>
</div>
</div>
</div>

<div class="toast" id="toast"></div>

<script>
let state = {users: [], keys: [], logs: [], stats: null, pendingAction: null};

function toast(msg, type='success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = 'toast ' + type, 3000);
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    const names = ['users', 'keys', 'logs', 'settings'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
}

async function loadAll() {
  try {
    const [s, u, k, l] = await Promise.all([
      fetch('/api/admin/stats').then(r => r.json()),
      fetch('/api/admin/users').then(r => r.json()),
      fetch('/api/admin/keys').then(r => r.json()),
      fetch('/api/admin/logs').then(r => r.json()),
    ]);
    state.stats = s;
    state.users = u.items || [];
    state.keys = k.items || [];
    state.logs = l.items || [];
    renderStats();
    renderUsers();
    renderKeys();
    renderLogs();
  } catch (e) {
    toast('❌ فشل التحميل', 'error');
  }
}

function renderStats() {
  const s = state.stats || {};
  document.getElementById('s-users').textContent = s.users ?? '-';
  document.getElementById('s-keys').textContent = s.keys_active ?? '-';
  document.getElementById('s-ops').textContent = s.operations ?? '-';
  document.getElementById('s-success').textContent = s.successful ?? '-';
  document.getElementById('s-banned').textContent = s.banned ?? '-';
  document.getElementById('s-credits').textContent = s.total_credits ?? '-';
}

function renderUsers() {
  const q = (document.getElementById('search-users').value || '').toLowerCase();
  const list = state.users.filter(u =>
    !q || String(u.id).toLowerCase().includes(q) || String(u.ip).toLowerCase().includes(q)
  );
  const el = document.getElementById('users-table');
  if (!list.length) { el.innerHTML = '<div class="empty">لا يوجد مستخدمون</div>'; return; }
  el.innerHTML = `<table><thead><tr>
    <th>المعرف</th><th>IP</th><th>النقاط</th><th>الاستخدام</th><th>البادئة</th><th>الحالة</th><th>إجراءات</th>
  </tr></thead><tbody>${list.map(u => `<tr>
    <td class="mono">${u.id.slice(0,10)}</td>
    <td style="color:var(--muted);font-size:.75rem">${u.ip}</td>
    <td><strong style="color:var(--cyan)">${u.credits}</strong></td>
    <td>${u.usage_count}</td>
    <td class="mono">${u.prefix}</td>
    <td>${u.is_banned ? '<span class="badge badge-ban">محظور</span>' : '<span class="badge badge-ok">نشط</span>'}</td>
    <td>
      <button class="btn btn-add" onclick="addCredits('${u.id}')">+ نقاط</button>
      <button class="btn btn-ban" onclick="toggleBan('${u.id}', ${!u.is_banned})">${u.is_banned ? 'فك حظر' : 'حظر'}</button>
      <button class="btn btn-del" onclick="deleteUser('${u.id}')">حذف</button>
    </td>
  </tr>`).join('')}</tbody></table>`;
}

function renderKeys() {
  const q = (document.getElementById('search-keys').value || '').toLowerCase();
  const list = state.keys.filter(k =>
    !q || String(k.prefix).toLowerCase().includes(q) || String(k.user_id).toLowerCase().includes(q)
  );
  const el = document.getElementById('keys-table');
  if (!list.length) { el.innerHTML = '<div class="empty">لا توجد مفاتيح</div>'; return; }
  el.innerHTML = `<table><thead><tr>
    <th>البادئة</th><th>المستخدم</th><th>الحالة</th><th>التاريخ</th><th>إجراءات</th>
  </tr></thead><tbody>${list.map(k => `<tr>
    <td class="mono">${k.prefix}</td>
    <td class="mono" style="font-size:.7rem">${String(k.user_id).slice(0,10)}</td>
    <td>${k.active ? '<span class="badge badge-ok">نشط</span>' : '<span class="badge badge-inactive">ملغى</span>'}</td>
    <td style="color:var(--muted);font-size:.7rem">${(k.created_at || '').slice(0,10)}</td>
    <td>
      ${k.active ? `<button class="btn btn-ban" onclick="revokeKey('${k.id}')">إلغاء</button>` : ''}
      <button class="btn btn-del" onclick="deleteKey('${k.id}')">حذف</button>
    </td>
  </tr>`).join('')}</tbody></table>`;
}

function renderLogs() {
  const el = document.getElementById('logs-table');
  if (!state.logs.length) { el.innerHTML = '<div class="empty">لا توجد عمليات</div>'; return; }
  el.innerHTML = `<table><thead><tr>
    <th>المستخدم</th><th>الإجراء</th><th>الحالة</th><th>المدة</th><th>التاريخ</th>
  </tr></thead><tbody>${state.logs.slice(0, 50).map(l => `<tr>
    <td class="mono" style="font-size:.7rem">${String(l.user_id || '').slice(0,10)}</td>
    <td><strong>${l.action || '?'}</strong></td>
    <td>${l.status === 'success' ? '<span class="badge badge-ok">✅</span>' : '<span class="badge badge-ban">❌</span>'}</td>
    <td style="color:var(--muted)">${l.duration_ms || 0}ms</td>
    <td style="color:var(--muted);font-size:.7rem">${(l.created_at || '').slice(0,16).replace('T', ' ')}</td>
  </tr>`).join('')}</tbody></table>`;
}

function openModal(title, desc, action, needAmount = false) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-desc').textContent = desc;
  document.getElementById('modal-amount').style.display = needAmount ? 'block' : 'none';
  document.getElementById('modal-amount').value = '';
  state.pendingAction = action;
  document.getElementById('modal').classList.add('show');
}

function closeModal() {
  document.getElementById('modal').classList.remove('show');
  state.pendingAction = null;
}

async function confirmModal() {
  if (!state.pendingAction) return;
  const action = state.pendingAction;
  const amount = parseInt(document.getElementById('modal-amount').value || '0');
  closeModal();
  await action(amount);
}

function addCredits(userId) {
  openModal('شحن نقاط', `أدخل عدد النقاط (سالب للخصم) للمستخدم ${userId.slice(0,10)}`, async (amount) => {
    if (!amount) return;
    const form = new FormData();
    form.append('user_id', userId);
    form.append('amount', amount);
    const r = await fetch('/api/admin/user/credits', {method: 'POST', body: form});
    if (r.ok) { toast('✅ تم التحديث'); await loadAll(); }
    else toast('❌ فشل', 'error');
  }, true);
}

function toggleBan(userId, ban) {
  openModal(ban ? 'حظر المستخدم' : 'فك الحظر', ban ? 'هل تريد حظر هذا المستخدم؟' : 'هل تريد فك الحظر؟', async () => {
    const form = new FormData();
    form.append('user_id', userId);
    form.append('ban', ban ? 'true' : 'false');
    const r = await fetch('/api/admin/user/ban', {method: 'POST', body: form});
    if (r.ok) { toast('✅ تم'); await loadAll(); }
    else toast('❌ فشل', 'error');
  });
}

function deleteUser(userId) {
  openModal('حذف المستخدم', '⚠️ حذف نهائي! كل مفاتيحه ستُلغى', async () => {
    const form = new FormData();
    form.append('user_id', userId);
    const r = await fetch('/api/admin/user/delete', {method: 'POST', body: form});
    if (r.ok) { toast('🗑️ تم الحذف'); await loadAll(); }
    else toast('❌ فشل', 'error');
  });
}

function revokeKey(keyId) {
  openModal('إلغاء المفتاح', 'لن يتمكن صاحبه من استخدامه بعد الآن', async () => {
    const form = new FormData();
    form.append('key_id', keyId);
    const r = await fetch('/api/admin/key/revoke', {method: 'POST', body: form});
    if (r.ok) { toast('✅ تم'); await loadAll(); }
    else toast('❌ فشل', 'error');
  });
}

function deleteKey(keyId) {
  openModal('حذف المفتاح', '⚠️ حذف نهائي!', async () => {
    const form = new FormData();
    form.append('key_id', keyId);
    const r = await fetch('/api/admin/key/delete', {method: 'POST', body: form});
    if (r.ok) { toast('🗑️ تم'); await loadAll(); }
    else toast('❌ فشل', 'error');
  });
}

async function changePassword(e) {
  e.preventDefault();
  const oldP = document.getElementById('old-pass').value;
  const newP = document.getElementById('new-pass').value;
  const form = new FormData();
  form.append('old_password', oldP);
  form.append('new_password', newP);
  const r = await fetch('/api/admin/change-password', {method: 'POST', body: form});
  if (r.ok) {
    toast('✅ تم تغيير كلمة المرور');
    document.getElementById('old-pass').value = '';
    document.getElementById('new-pass').value = '';
  } else {
    const d = await r.json().catch(() => ({}));
    toast('❌ ' + (d.detail || 'فشل'), 'error');
  }
}

loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
#  [13] Main user-facing page (نفس النسخة السابقة مع إضافة زر admin)
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
:root{--bg:#05070f;--bg2:#0a0e1a;--surface:rgba(255,255,255,.035);--surface2:rgba(255,255,255,.06);--border:rgba(255,255,255,.08);--text:#e8ecf4;--muted:#8a94a8;--cyan:#00e0ff;--purple:#7c5cff;--pink:#ff2d95;--success:#00ffa3;--danger:#ff4d6d;--warn:#ffb020;--grad:linear-gradient(135deg,#00e0ff 0%,#7c5cff 50%,#ff2d95 100%)}
body.light{--bg:#f5f7fb;--bg2:#e8ecf4;--surface:#fff;--surface2:#f0f3f8;--border:rgba(0,0,0,.08);--text:#0a0e1a;--muted:#5a6478}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Tajawal',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(circle at 15% 20%,rgba(0,224,255,.12),transparent 35%),radial-gradient(circle at 85% 15%,rgba(124,92,255,.14),transparent 35%),radial-gradient(circle at 50% 90%,rgba(255,45,149,.09),transparent 40%)}
body.light::before{opacity:.4}
header{position:sticky;top:0;z-index:100;backdrop-filter:blur(20px);background:rgba(5,7,15,.75);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between}
body.light header{background:rgba(245,247,251,.85)}
.logo{font-weight:900;font-size:1.3rem}
.logo .dot{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.nav{display:flex;gap:10px;align-items:center}
.nav a{color:var(--muted);font-weight:500;font-size:.85rem;text-decoration:none}
.nav a:hover{color:var(--text)}
.icon-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;cursor:pointer;font-size:1rem;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}
.icon-btn:hover{background:rgba(255,255,255,.12)}
.badge{padding:4px 10px;border-radius:20px;font-size:.72rem;font-weight:700;background:rgba(0,255,163,.15);color:var(--success);border:1px solid rgba(0,255,163,.3)}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}
.hero{text-align:center;padding:30px 0 40px}
.hero h1{font-size:clamp(1.8rem,5vw,3.2rem);font-weight:900;line-height:1.15;letter-spacing:-1.2px;margin-bottom:16px}
.hero h1 .grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{font-size:1.05rem;color:var(--muted);max-width:560px;margin:0 auto 28px}
.btn{display:inline-flex;align-items:center;gap:8px;justify-content:center;padding:13px 24px;border-radius:12px;border:none;cursor:pointer;font-family:inherit;font-size:.95rem;font-weight:700;transition:all .2s}
.btn-primary{background:var(--grad);color:#05070f;box-shadow:0 8px 30px rgba(124,92,255,.4)}
.btn-primary:hover{transform:translateY(-2px)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:24px;backdrop-filter:blur(10px)}
.key-banner{background:linear-gradient(135deg,rgba(0,224,255,.08),rgba(124,92,255,.08));border:1px solid rgba(0,224,255,.25);border-radius:18px;padding:22px;margin-bottom:24px;text-align:center}
.key-display{display:flex;gap:8px;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px;align-items:center;font-family:'JetBrains Mono',monospace;font-size:.82rem;margin-bottom:10px;word-break:break-all}
.key-display span{flex:1;color:var(--cyan);text-align:left;direction:ltr}
.key-display button{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:7px 12px;border-radius:8px;cursor:pointer;font-weight:700;font-size:.78rem}
.section{margin-bottom:28px}
.section h2{font-size:1.4rem;margin-bottom:16px;font-weight:700}
.upload-zone{border:2px dashed var(--border);border-radius:18px;padding:50px 20px;text-align:center;cursor:pointer;transition:all .25s;background:var(--surface)}
.upload-zone:hover,.upload-zone.dragover{border-color:var(--cyan);background:rgba(0,224,255,.05)}
.upload-zone svg{width:48px;height:48px;stroke:var(--cyan);margin-bottom:12px}
.upload-zone input{display:none}
.preview-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px}
@media(max-width:600px){.preview-row{grid-template-columns:1fr}}
.img-box{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:12px;text-align:center}
.img-box .label{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:10px}
.img-box img{max-width:100%;max-height:360px;border-radius:8px;display:block;margin:0 auto}
.checker{background-image:linear-gradient(45deg,#1a1f2e 25%,transparent 25%),linear-gradient(-45deg,#1a1f2e 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#1a1f2e 75%),linear-gradient(-45deg,transparent 75%,#1a1f2e 75%);background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0;background-color:#0d1220;border-radius:8px;padding:6px}
.tools-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin-top:16px}
.tool-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:16px 8px;border-radius:12px;cursor:pointer;font-family:inherit;font-weight:700;font-size:.85rem;transition:all .2s;display:flex;flex-direction:column;align-items:center;gap:6px}
.tool-btn:hover:not(:disabled){background:rgba(0,224,255,.1);border-color:var(--cyan);transform:translateY(-2px)}
.tool-btn .ic{font-size:1.5rem}
.loading{display:flex;flex-direction:column;align-items:center;padding:40px;gap:14px}
.spinner{width:44px;height:44px;border-radius:50%;border:4px solid var(--border);border-top-color:var(--cyan);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:500px){.stats-grid{grid-template-columns:1fr}}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center}
.stat .num{font-size:1.5rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:'JetBrains Mono',monospace}
.stat .lbl{font-size:.78rem;color:var(--muted);margin-top:2px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(150%);background:var(--bg2);border:1px solid var(--border);padding:12px 22px;border-radius:12px;z-index:999;font-weight:600;font-size:.9rem;transition:transform .3s;max-width:calc(100% - 32px)}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}
.hidden{display:none !important}
footer{text-align:center;padding:28px 16px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);margin-top:40px}
.history-item{display:flex;justify-content:space-between;align-items:center;padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;font-size:.85rem}
</style>
</head>
<body>
<header>
<div class="logo">Lux<span class="dot">.</span>Studio <span style="font-size:.7rem;color:var(--warn);font-weight:900">VIP</span></div>
<nav class="nav">
<a href="#studio">الاستوديو</a>
<button class="icon-btn" onclick="toggleTheme()" id="theme-btn">🌙</button>
<a href="/admin" class="icon-btn" title="لوحة المدير">👑</a>
<span class="badge" id="status-badge">● جاهز</span>
</nav>
</header>
<main>
<section class="hero">
<h1>استوديو احترافي<br><span class="grad">VIP Edition</span></h1>
<p>10 أدوات صور — إزالة خلفيات، تحسين، تحويل، وكل ما تحتاجه</p>
<div id="hero-actions">
<button class="btn btn-primary" id="get-key-btn" onclick="getFreeKey()">🚀 احصل على مفتاح مجاني — 25 عملية</button>
<button class="btn btn-primary hidden" id="start-btn" onclick="scrollToStudio()">✨ ابدأ الآن</button>
</div>
</section>
<div class="key-banner hidden" id="new-key-banner">
<h3>🎉 تم إنشاء مفتاحك</h3>
<p style="color:var(--muted);margin-bottom:16px">احفظه الآن — لن يظهر مرة أخرى</p>
<div class="key-display"><span id="new-key-value">-</span><button onclick="copyNewKey()">📋 نسخ</button></div>
<p style="color:var(--warn);font-size:.8rem">⚠️ فقدانه يعني عدم القدرة على الاستخدام</p>
</div>
<section class="section hidden" id="api-panel">
<h2>🔑 لوحة API</h2>
<div class="card">
<div class="key-display" style="margin-bottom:16px">
<span id="api-key-masked">-</span>
<button onclick="toggleKeyVisibility()" id="toggle-key-btn">👁</button>
<button onclick="copyApiKey()">📋</button>
</div>
<div class="stats-grid">
<div class="stat"><div class="num" id="stat-credits">-</div><div class="lbl">نقطة متبقية</div></div>
<div class="stat"><div class="num" id="stat-ops">-</div><div class="lbl">إجمالي العمليات</div></div>
<div class="stat"><div class="num" id="stat-success">-</div><div class="lbl">ناجحة</div></div>
</div>
</div>
</section>
<section class="section hidden" id="studio">
<h2>🎨 الاستوديو</h2>
<div class="upload-zone" id="drop-zone">
<input type="file" id="file-input" accept="image/*">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اسحب صورة أو اضغط للاختيار</h3>
<p style="color:var(--muted);font-size:.85rem">PNG · JPG · WEBP — حتى 15 ميجابايت</p>
</div>
<div class="hidden" id="workspace">
<div class="preview-row">
<div class="img-box"><div class="label">الأصلية</div><img id="original-img" alt=""></div>
<div class="img-box hidden" id="result-box">
<div class="label" id="result-label">النتيجة</div>
<div class="checker" id="result-checker"><img id="result-img" alt=""></div>
</div>
</div>
<div class="tools-grid" id="tools-grid">
<button class="tool-btn" onclick="runAction('remove-bg')"><span class="ic">🖼️</span>إزالة الخلفية</button>
<button class="tool-btn" onclick="runAction('enhance')"><span class="ic">✨</span>تحسين</button>
<button class="tool-btn" onclick="runAction('compress',{quality:70})"><span class="ic">📉</span>ضغط</button>
<button class="tool-btn" onclick="runAction('grayscale')"><span class="ic">⚫</span>رمادي</button>
<button class="tool-btn" onclick="runAction('sepia')"><span class="ic">🌅</span>سيبيا</button>
<button class="tool-btn" onclick="runAction('invert')"><span class="ic">🌗</span>انعكاس</button>
<button class="tool-btn" onclick="runAction('rotate',{angle:90})"><span class="ic">🔃</span>تدوير</button>
<button class="tool-btn" onclick="runAction('flip',{direction:'h'})"><span class="ic">↔️</span>قلب</button>
<button class="tool-btn" onclick="runAction('blur',{radius:5})"><span class="ic">💨</span>بلور</button>
<button class="tool-btn" onclick="runAction('convert',{target:'png'})"><span class="ic">🔄</span>PNG</button>
<button class="tool-btn" onclick="runAction('convert',{target:'webp'})"><span class="ic">🔄</span>WEBP</button>
<button class="tool-btn" onclick="resetStudio()"><span class="ic">🆕</span>صورة جديدة</button>
</div>
<div class="hidden" id="loading"><div class="loading"><div class="spinner"></div><p>⏳ جاري المعالجة...</p></div></div>
<div class="hidden" id="download-row" style="text-align:center;margin-top:16px"><a id="download-link" class="btn btn-primary" download>⬇️ تحميل النتيجة</a></div>
</div>
</section>
<section class="section hidden" id="history-section">
<h2>📜 سجلّك</h2>
<div class="card"><div id="history-list" style="max-height:400px;overflow-y:auto">-</div></div>
</section>
</main>
<footer>Lux Studio VIP © 2025 — Premium Edition ✨</footer>
<div class="toast" id="toast"></div>
<script>
const state={key:localStorage.getItem('lux_api_key')||null,lastFile:null,keyVisible:false,theme:localStorage.getItem('lux_theme')||'dark'};
function toast(msg,type='success'){const el=document.getElementById('toast');el.textContent=msg;el.className='toast show '+type;clearTimeout(el._t);el._t=setTimeout(()=>el.className='toast '+type,3500)}
function applyTheme(){document.body.classList.toggle('light',state.theme==='light');document.getElementById('theme-btn').textContent=state.theme==='light'?'☀️':'🌙'}
function toggleTheme(){state.theme=state.theme==='dark'?'light':'dark';localStorage.setItem('lux_theme',state.theme);applyTheme()}
window.addEventListener('DOMContentLoaded',()=>{applyTheme();if(state.key){showStudio();refreshUser();loadHistory()}else showWelcome();checkHealth();setInterval(checkHealth,60000)});
async function checkHealth(){try{const r=await fetch('/api/health');const d=await r.json();const b=document.getElementById('status-badge');if(d.status==='ok'){b.textContent='● متصل';b.style.color='var(--success)'}else{b.textContent='● DB';b.style.color='var(--warn)'}}catch(e){}}
function showWelcome(){document.getElementById('studio').classList.add('hidden');document.getElementById('api-panel').classList.add('hidden');document.getElementById('history-section').classList.add('hidden');document.getElementById('get-key-btn').classList.remove('hidden');document.getElementById('start-btn').classList.add('hidden')}
function showStudio(){document.getElementById('studio').classList.remove('hidden');document.getElementById('api-panel').classList.remove('hidden');document.getElementById('history-section').classList.remove('hidden');document.getElementById('get-key-btn').classList.add('hidden');document.getElementById('start-btn').classList.remove('hidden')}
function scrollToStudio(){document.getElementById('studio').scrollIntoView({behavior:'smooth'})}
async function getFreeKey(){const b=document.getElementById('get-key-btn');b.disabled=true;const o=b.textContent;b.textContent='⏳ جاري...';try{const r=await fetch('/api/keys/new',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'فشل');state.key=d.api_key;localStorage.setItem('lux_api_key',d.api_key);document.getElementById('new-key-value').textContent=d.api_key;document.getElementById('new-key-banner').classList.remove('hidden');showStudio();await refreshUser();await loadHistory();document.getElementById('new-key-banner').scrollIntoView({behavior:'smooth',block:'center'});toast('✅ تم إنشاء المفتاح')}catch(e){toast('❌ '+e.message,'error');b.disabled=false;b.textContent=o}}
function copyNewKey(){navigator.clipboard.writeText(document.getElementById('new-key-value').textContent).then(()=>toast('📋 تم النسخ'))}
function maskKey(k){return k?k.slice(0,10)+'••••••'+k.slice(-4):'-'}
function updateKeyDisplay(){const el=document.getElementById('api-key-masked');el.textContent=state.keyVisible?state.key:maskKey(state.key);document.getElementById('toggle-key-btn').textContent=state.keyVisible?'🙈':'👁'}
function toggleKeyVisibility(){state.keyVisible=!state.keyVisible;updateKeyDisplay()}
function copyApiKey(){if(state.key)navigator.clipboard.writeText(state.key).then(()=>toast('📋 تم النسخ'))}
async function refreshUser(){if(!state.key)return;try{const r=await fetch('/api/me',{headers:{'X-API-Key':state.key}});if(r.status===401){localStorage.removeItem('lux_api_key');state.key=null;showWelcome();toast('⚠️ مفتاحك غير صالح','error');return}if(!r.ok)return;const d=await r.json();document.getElementById('stat-credits').textContent=d.credits;document.getElementById('stat-ops').textContent=d.total_operations;document.getElementById('stat-success').textContent=d.successful;updateKeyDisplay()}catch(e){}}
async function loadHistory(){if(!state.key)return;try{const r=await fetch('/api/history',{headers:{'X-API-Key':state.key}});if(!r.ok)return;const d=await r.json();const el=document.getElementById('history-list');if(!d.items||!d.items.length){el.textContent='لا يوجد سجل بعد';return}el.innerHTML=d.items.slice(0,20).map(x=>`<div class="history-item"><span>${x.action||'?'}</span><span style="color:${x.status==='success'?'var(--success)':'var(--danger)'}">${x.status==='success'?'✅':'❌'}</span><span style="color:var(--muted);font-size:.75rem">${(x.created_at||'').slice(0,16).replace('T',' ')}</span></div>`).join('')}catch(e){}}
const dropZone=document.getElementById('drop-zone');const fileInput=document.getElementById('file-input');
dropZone.addEventListener('click',()=>fileInput.click());
dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('dragover')});
dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('dragover');if(e.dataTransfer.files.length)handleFile(e.dataTransfer.files[0])});
fileInput.addEventListener('change',e=>{if(e.target.files.length)handleFile(e.target.files[0])});
function handleFile(file){if(!file.type.startsWith('image/'))return toast('❌ ليس صورة','error');if(file.size>15*1024*1024)return toast('❌ أكبر من 15MB','error');state.lastFile=file;const r=new FileReader();r.onload=e=>{document.getElementById('original-img').src=e.target.result;document.getElementById('workspace').classList.remove('hidden');document.getElementById('result-box').classList.add('hidden');document.getElementById('download-row').classList.add('hidden');document.getElementById('workspace').scrollIntoView({behavior:'smooth',block:'start'})};r.readAsDataURL(file)}
async function runAction(action,params={}){if(!state.lastFile)return toast('❌ اختر صورة','error');if(!state.key)return toast('❌ مفتاح مفقود','error');const grid=document.getElementById('tools-grid');const ld=document.getElementById('loading');const rb=document.getElementById('result-box');const dr=document.getElementById('download-row');grid.classList.add('hidden');ld.classList.remove('hidden');rb.classList.add('hidden');dr.classList.add('hidden');const t0=performance.now();try{const form=new FormData();form.append('file',state.lastFile);const qs=new URLSearchParams(params).toString();const url='/api/'+action+(qs?'?'+qs:'');const r=await fetch(url,{method:'POST',headers:{'X-API-Key':state.key},body:form});if(!r.ok){const err=await r.json().catch(()=>({detail:'خطأ '+r.status}));throw new Error(err.detail||'فشل')}const blob=await r.blob();const objUrl=URL.createObjectURL(blob);document.getElementById('result-img').src=objUrl;document.getElementById('result-label').textContent=action==='remove-bg'?'بدون خلفية':action==='enhance'?'محسّنة':'النتيجة';document.getElementById('result-checker').style.background=action==='remove-bg'?'':'transparent';rb.classList.remove('hidden');const dl=document.getElementById('download-link');const ext=blob.type==='image/png'?'png':blob.type==='image/webp'?'webp':'jpg';dl.href=objUrl;dl.download=`lux_${action}.${ext}`;dr.classList.remove('hidden');grid.classList.remove('hidden');const dt=Math.round(performance.now()-t0);toast(`✅ تمت في ${dt}ms`);await refreshUser();await loadHistory()}catch(e){toast('❌ '+e.message,'error');grid.classList.remove('hidden')}finally{ld.classList.add('hidden')}}
function resetStudio(){state.lastFile=null;fileInput.value='';document.getElementById('workspace').classList.add('hidden');dropZone.scrollIntoView({behavior:'smooth',block:'center'})}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
#  [14] Assets
# ═══════════════════════════════════════════════════════════════════
SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#00e0ff"/><stop offset="0.5" stop-color="#7c5cff"/><stop offset="1" stop-color="#ff2d95"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="#05070f"/><circle cx="256" cy="256" r="140" fill="none" stroke="url(#g)" stroke-width="20"/><path d="M200 320 L256 200 L312 320 Z" fill="url(#g)"/><circle cx="256" cy="180" r="22" fill="url(#g)"/></svg>"""

SW_JS = """self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;if(e.request.url.includes('/api/'))return;if(e.request.url.endsWith('/ping'))return;e.respondWith(caches.open('lux-v5').then(c=>fetch(e.request).then(r=>{if(r.ok&&r.type==='basic')c.put(e.request,r.clone());return r}).catch(()=>c.match(e.request))))});"""


# ═══════════════════════════════════════════════════════════════════
#  [15] Run
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"👑 Lux Studio VIP v5.0 → port {PORT}", flush=True)
    print(f"📦 DSS: {DSS_BASE_URL}", flush=True)
    print(f"🔐 Admin: /admin  (password: {ADMIN_PASSWORD})", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
