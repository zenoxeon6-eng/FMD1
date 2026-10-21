"""
╔══════════════════════════════════════════════════════════════════════╗
║  👑 LUX STUDIO VIP v6 — Complete Designer App                        ║
║  ──────────────────────────────────────────────────────────────────  ║
║  • Sidebar drawer navigation (9 pages)                               ║
║  • 17 designer tools (remove-bg, enhance, crop, resize, filters...)  ║
║  • API Keys page                                                     ║
║  • Admin panel (password: xeon)                                      ║
║  • PWA installable — feels like real APK                             ║
║  • DSS database integrated                                           ║
╚══════════════════════════════════════════════════════════════════════╝
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
ADMIN_PASSWORD = "xeon"
ADMIN_SESSION_HOURS = 24
FREE_CREDITS = 30
MAX_FILE_MB = 15
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
PORT = int(os.getenv("PORT", "8000"))
RATE_WINDOW = 60
RATE_LIMIT = 40

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout, force=True,
)
log = logging.getLogger("lux")

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
            log.info(f"✅ Model '{REMBG_MODEL}' ready")
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
#  DSS CLIENT
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
                last = DSSError(f"net {method} {path}: {e}")
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
#  RATE LIMITER + ADMIN SESSIONS
# ═══════════════════════════════════════════════════════════════════
_rate_buckets: dict = defaultdict(list)
_admin_sessions: dict = {}


def rate_check(key: str) -> bool:
    now = time.time()
    _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < RATE_WINDOW]
    if len(_rate_buckets[key]) >= RATE_LIMIT: return False
    _rate_buckets[key].append(now)
    return True


def new_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = time.time() + ADMIN_SESSION_HOURS * 3600
    now = time.time()
    for t in list(_admin_sessions.keys()):
        if _admin_sessions[t] < now: _admin_sessions.pop(t, None)
    return token


def check_admin_session(token: Optional[str]) -> bool:
    if not token: return False
    exp = _admin_sessions.get(token)
    if not exp: return False
    if exp < time.time():
        _admin_sessions.pop(token, None); return False
    return True


def require_admin(lux_admin: Optional[str] = Cookie(None)):
    if not check_admin_session(lux_admin):
        raise HTTPException(401, "Admin session required")

# ═══════════════════════════════════════════════════════════════════
#  USERS
# ═══════════════════════════════════════════════════════════════════
def sha256(s: str) -> str: return hashlib.sha256(s.encode()).hexdigest()


async def get_user_by_id(uid: str) -> Optional[dict]:
    for u in await dss.list("users"):
        if str(u.get("id")) == str(uid) or str(u.get("_id")) == str(uid):
            return u
    return None


async def find_api_key(plain: str) -> Optional[dict]:
    target = sha256(plain.strip())
    try: keys = await dss.list("api_keys")
    except DSSError as e:
        log.error(f"find_api_key: {e}"); return None
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
        "id": secrets.token_urlsafe(12), "user_id": user_id,
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
        }); return True
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
            "id": secrets.token_urlsafe(12), "user_id": user_id,
            "action": action, "status": status,
            "duration_ms": duration_ms, "size_bytes": size_bytes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except: pass


async def authenticate(x_api_key: Optional[str]) -> dict:
    if not x_api_key: raise HTTPException(401, "X-API-Key required")
    rec = await find_api_key(x_api_key.strip())
    if not rec: raise HTTPException(401, "Invalid or revoked key")
    user = await get_user_by_id(rec.get("user_id"))
    if user and user.get("is_banned"): raise HTTPException(403, "Account banned")
    return rec

# ═══════════════════════════════════════════════════════════════════
#  IMAGE TOOLS (17 tools)
# ═══════════════════════════════════════════════════════════════════
def _open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data)); img.load(); return img
    except Exception as e:
        raise ValueError(f"Invalid image: {e}")


def _save(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    out = io.BytesIO(); img.save(out, format=fmt, **kw); return out.getvalue()


def t_remove_bg(data: bytes) -> tuple:
    if _rembg_remove is None: raise RuntimeError("Model loading... retry in 30s")
    try: result = _rembg_remove(data, model_name=REMBG_MODEL)
    except: result = _rembg_remove(data)
    return result, "image/png", "png"


def t_enhance(data: bytes) -> tuple:
    img = _open_image(data)
    has_alpha = img.mode in ("RGBA","LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if has_alpha else "RGB")
    w, h = img.size
    nw, nh = w * 2, h * 2
    if max(nw, nh) > 2048:
        r = 2048 / max(nw, nh); nw, nh = int(nw * r), int(nh * r)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    img = ImageEnhance.Sharpness(img).enhance(1.4)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.18)
    if has_alpha: return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_compress(data: bytes, quality: int = 75) -> tuple:
    img = _open_image(data)
    q = max(10, min(95, quality))
    if img.mode not in ("RGB","RGBA"):
        img = img.convert("RGBA" if img.mode in ("P","LA") else "RGB")
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=q, optimize=True), "image/jpeg", "jpg"


def t_convert(data: bytes, target: str = "png") -> tuple:
    img = _open_image(data); target = target.lower()
    if target not in ("jpg","jpeg","png","webp"): raise ValueError(f"Unsupported: {target}")
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


def t_grayscale(data: bytes) -> tuple:
    img = _open_image(data)
    if img.mode in ("RGBA","LA"):
        alpha = img.split()[-1]
        gray = ImageOps.grayscale(img.convert("RGB")).convert("RGBA")
        gray.putalpha(alpha); img = gray
    else: img = ImageOps.grayscale(img.convert("RGB"))
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_sepia(data: bytes) -> tuple:
    img = _open_image(data).convert("RGB")
    pixels = img.load(); w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            tr = int(0.393*r + 0.769*g + 0.189*b)
            tg = int(0.349*r + 0.686*g + 0.168*b)
            tb = int(0.272*r + 0.534*g + 0.131*b)
            pixels[x, y] = (min(255,tr), min(255,tg), min(255,tb))
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_invert(data: bytes) -> tuple:
    img = _open_image(data)
    if img.mode == "RGBA":
        r, g, b, a = img.split()
        inv = ImageOps.invert(Image.merge("RGB", (r, g, b)))
        r2, g2, b2 = inv.split()
        img = Image.merge("RGBA", (r2, g2, b2, a))
        return _save(img, "PNG", optimize=True), "image/png", "png"
    img = img.convert("RGB")
    return _save(ImageOps.invert(img), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_rotate(data: bytes, angle: int = 90) -> tuple:
    img = _open_image(data); angle = angle % 360
    img = img.rotate(-angle, expand=True, fillcolor=(0,0,0,0) if img.mode == "RGBA" else None)
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_flip(data: bytes, direction: str = "h") -> tuple:
    img = _open_image(data)
    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT if direction == "h" else Image.Transpose.FLIP_TOP_BOTTOM)
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_blur(data: bytes, radius: int = 5) -> tuple:
    img = _open_image(data)
    r = max(1, min(30, radius))
    img = img.filter(ImageFilter.GaussianBlur(radius=r))
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_sharpen(data: bytes) -> tuple:
    img = _open_image(data)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=2))
    img = ImageEnhance.Sharpness(img).enhance(1.8)
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=95, optimize=True), "image/jpeg", "jpg"


def t_vintage(data: bytes) -> tuple:
    img = _open_image(data).convert("RGB")
    img = ImageEnhance.Color(img).enhance(0.6)
    img = ImageEnhance.Contrast(img).enhance(1.1)
    img = img.convert("RGB")
    r, g, b = img.split()
    r = r.point(lambda i: min(255, i + 15))
    b = b.point(lambda i: max(0, i - 15))
    img = Image.merge("RGB", (r, g, b))
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_warm(data: bytes) -> tuple:
    img = _open_image(data).convert("RGB")
    r, g, b = img.split()
    r = r.point(lambda i: min(255, i + 20))
    b = b.point(lambda i: max(0, i - 20))
    img = Image.merge("RGB", (r, g, b))
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_cool(data: bytes) -> tuple:
    img = _open_image(data).convert("RGB")
    r, g, b = img.split()
    r = r.point(lambda i: max(0, i - 20))
    b = b.point(lambda i: min(255, i + 20))
    img = Image.merge("RGB", (r, g, b))
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_adjust(data: bytes, brightness: int = 0, contrast: int = 0, saturation: int = 0) -> tuple:
    img = _open_image(data)
    has_alpha = img.mode in ("RGBA","LA")
    if has_alpha:
        alpha = img.split()[-1] if img.mode == "RGBA" else None
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")
    b_factor = 1.0 + brightness / 100.0
    c_factor = 1.0 + contrast / 100.0
    s_factor = 1.0 + saturation / 100.0
    img = ImageEnhance.Brightness(img).enhance(max(0.1, b_factor))
    img = ImageEnhance.Contrast(img).enhance(max(0.1, c_factor))
    img = ImageEnhance.Color(img).enhance(max(0.0, s_factor))
    if has_alpha:
        r, g, b = img.split()
        img = Image.merge("RGBA", (r, g, b, alpha)) if alpha else img.convert("RGBA")
        return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_resize(data: bytes, width: int = 0, height: int = 0) -> tuple:
    img = _open_image(data)
    w, h = img.size
    if width <= 0 and height <= 0: raise ValueError("Specify width or height")
    if width > 0 and height > 0:
        nw, nh = width, height
    elif width > 0:
        nw = width; nh = int(h * width / w)
    else:
        nh = height; nw = int(w * height / h)
    nw, nh = min(nw, 4096), min(nh, 4096)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_crop(data: bytes, ratio: str = "1:1") -> tuple:
    img = _open_image(data); w, h = img.size
    try: rw, rh = map(int, ratio.split(":"))
    except: rw, rh = 1, 1
    target_ratio = rw / rh
    current = w / h
    if current > target_ratio:
        nh = h; nw = int(h * target_ratio); x, y = (w - nw) // 2, 0
    else:
        nw = w; nh = int(w / target_ratio); x, y = 0, (h - nh) // 2
    img = img.crop((x, y, x + nw, y + nh))
    if img.mode == "RGBA": return _save(img, "PNG", optimize=True), "image/png", "png"
    return _save(img, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_watermark(data: bytes, text: str = "Lux Studio", position: str = "br", opacity: int = 60) -> tuple:
    img = _open_image(data).convert("RGBA")
    txt_layer = Image.new("RGBA", img.size, (0,0,0,0))
    draw = ImageDraw.Draw(txt_layer)
    font = None
    try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(20, img.width // 20))
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0,0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = 20
    positions = {
        "tl": (margin, margin),
        "tr": (img.width - tw - margin, margin),
        "bl": (margin, img.height - th - margin),
        "br": (img.width - tw - margin, img.height - th - margin),
        "c": ((img.width - tw) // 2, (img.height - th) // 2),
    }
    pos = positions.get(position, positions["br"])
    draw.text(pos, text, font=font, fill=(255, 255, 255, int(255 * opacity / 100)))
    out = Image.alpha_composite(img, txt_layer)
    return _save(out, "PNG", optimize=True), "image/png", "png"


def t_bg_color(data: bytes, color: str = "#ffffff") -> tuple:
    img = _open_image(data).convert("RGBA")
    hex_c = color.lstrip("#")
    try:
        r, g, b = int(hex_c[0:2], 16), int(hex_c[2:4], 16), int(hex_c[4:6], 16)
    except: r, g, b = 255, 255, 255
    bg = Image.new("RGBA", img.size, (r, g, b, 255))
    out = Image.alpha_composite(bg, img)
    return _save(out.convert("RGB"), "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


def t_round_corners(data: bytes, radius: int = 40) -> tuple:
    img = _open_image(data).convert("RGBA")
    w, h = img.size
    radius = min(radius, min(w, h) // 2)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    img.putalpha(mask)
    return _save(img, "PNG", optimize=True), "image/png", "png"


def t_border(data: bytes, thickness: int = 20, color: str = "#00e0ff") -> tuple:
    img = _open_image(data)
    has_alpha = img.mode == "RGBA"
    if not has_alpha: img = img.convert("RGB")
    hex_c = color.lstrip("#")
    try: rgb = (int(hex_c[0:2],16), int(hex_c[2:4],16), int(hex_c[4:6],16))
    except: rgb = (0, 224, 255)
    out = ImageOps.expand(img, border=thickness, fill=rgb if not has_alpha else (*rgb, 255))
    if out.mode == "RGBA": return _save(out, "PNG", optimize=True), "image/png", "png"
    return _save(out, "JPEG", quality=92, optimize=True), "image/jpeg", "jpg"


# Registry
TOOLS = {
    "remove-bg": {"fn": t_remove_bg, "label": "إزالة الخلفية", "icon": "🖼️", "desc": "خلفية شفافة في ثوانٍ"},
    "enhance": {"fn": t_enhance, "label": "تحسين", "icon": "✨", "desc": "تكبير ×2 + حدة + ألوان"},
    "compress": {"fn": t_compress, "label": "ضغط", "icon": "📉", "desc": "حجم أصغر، جودة عالية"},
    "convert": {"fn": t_convert, "label": "تحويل", "icon": "🔄", "desc": "PNG / JPG / WEBP"},
    "grayscale": {"fn": t_grayscale, "label": "رمادي", "icon": "⚫", "desc": "أبيض وأسود احترافي"},
    "sepia": {"fn": t_sepia, "label": "سيبيا", "icon": "🌅", "desc": "لون عتيق دافئ"},
    "invert": {"fn": t_invert, "label": "انعكاس", "icon": "🌗", "desc": "ألوان معكوسة"},
    "rotate": {"fn": t_rotate, "label": "تدوير", "icon": "🔃", "desc": "0° / 90° / 180° / 270°"},
    "flip": {"fn": t_flip, "label": "قلب", "icon": "↔️", "desc": "أفقي أو رأسي"},
    "blur": {"fn": t_blur, "label": "بلور", "icon": "💨", "desc": "تنعيم الخلفية"},
    "sharpen": {"fn": t_sharpen, "label": "حدّة", "icon": "🔪", "desc": "تفاصيل أوضح"},
    "vintage": {"fn": t_vintage, "label": "فينتاج", "icon": "📷", "desc": "فلتر كلاسيكي"},
    "warm": {"fn": t_warm, "label": "دافئ", "icon": "☀️", "desc": "ألوان حمراء دافئة"},
    "cool": {"fn": t_cool, "label": "بارد", "icon": "❄️", "desc": "ألوان زرقاء باردة"},
    "adjust": {"fn": t_adjust, "label": "ضبط", "icon": "🎚️", "desc": "سطوع/تباين/تشبع"},
    "resize": {"fn": t_resize, "label": "تحجيم", "icon": "📐", "desc": "أبعاد مخصصة"},
    "crop": {"fn": t_crop, "label": "قص", "icon": "✂️", "desc": "نسبة عرض/ارتفاع"},
    "watermark": {"fn": t_watermark, "label": "علامة مائية", "icon": "💧", "desc": "نص فوق الصورة"},
    "bg-color": {"fn": t_bg_color, "label": "خلفية لون", "icon": "🎨", "desc": "بديل شفاف"},
    "round-corners": {"fn": t_round_corners, "label": "زوايا دائرية", "icon": "⭕", "desc": "شكل أنيق"},
    "border": {"fn": t_border, "label": "إطار", "icon": "🖼️", "desc": "حدود ملونة"},
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
                await c.get(f"{url}/ping")
        except: pass


@contextlib.asynccontextmanager
async def lifespan(app):
    bg = asyncio.create_task(_ensure_rembg_loaded())
    ka = asyncio.create_task(_keep_alive())
    log.info("🚀 Lux Studio VIP v6 ready")
    try: yield
    finally:
        for t in (bg, ka): t.cancel()
        for t in (bg, ka):
            with contextlib.suppress(asyncio.CancelledError, Exception): await t

# ═══════════════════════════════════════════════════════════════════
#  FASTAPI
# ═══════════════════════════════════════════════════════════════════
app = FastAPI(title="Lux Studio VIP", version="6.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
async def index(): return HTML_PAGE


@app.get("/ping")
async def ping(): return {"status": "alive", "ts": time.time(), "rembg_ready": _rembg_ready}


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Lux Studio VIP", "short_name": "Lux VIP", "start_url": "/",
        "display": "standalone", "orientation": "portrait-primary",
        "background_color": "#05070f", "theme_color": "#00e0ff",
        "description": "17 أداة مصمم احترافية",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
    })


@app.get("/icon.svg")
async def icon(): return Response(content=SVG_ICON, media_type="image/svg+xml")


@app.get("/sw.js")
async def sw(): return Response(content=SW_JS, media_type="application/javascript")


@app.get("/api/health")
async def health():
    r = {"status": "ok", "rembg_ready": _rembg_ready, "version": "6.0.0"}
    try:
        users = await dss.list("users"); r["dss"] = "connected"; r["dss_users"] = len(users)
    except Exception as e:
        r["dss"] = f"error: {e}"; r["status"] = "degraded"
    return r


# ─── Keys ───
@app.post("/api/keys/new")
async def api_new_key(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not rate_check(f"key:{ip}"): raise HTTPException(429, "Too many attempts")
    try:
        if await ip_key_count(ip) >= 3: raise HTTPException(429, "Max 3 keys/day")
        return {**await create_free_key(ip), "message": "Save this key now"}
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
        "prefix": rec.get("prefix"), "credits": int(user.get("credits", 0)),
        "total_operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
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


# ─── Image processing ───
async def _run_tool(tool_name: str, file: UploadFile, rec: dict, params: dict = None):
    params = params or {}
    user = await get_user_by_id(rec["user_id"])
    if not user or int(user.get("credits", 0)) < 1: raise HTTPException(402, "Out of credits")
    if not rate_check(f"user:{rec['user_id']}"): raise HTTPException(429, "Slow down")

    data = await file.read()
    if len(data) == 0: raise HTTPException(400, "Empty file")
    if len(data) > MAX_FILE_BYTES: raise HTTPException(413, f"Max {MAX_FILE_MB}MB")

    if tool_name == "remove-bg" and not _rembg_ready:
        try: await asyncio.wait_for(_rembg_event.wait(), timeout=120.0)
        except asyncio.TimeoutError: raise HTTPException(503, "Model loading")

    t0 = time.time()
    tool = TOOLS[tool_name]
    try:
        result = await asyncio.to_thread(tool["fn"], data, **params)
    except ValueError as e:
        await log_usage(rec["user_id"], tool_name, "error", 0)
        raise HTTPException(400, str(e))
    except Exception as e:
        await log_usage(rec["user_id"], tool_name, "error", 0)
        raise HTTPException(500, str(e))

    media_type = "image/png"; ext = "png"
    if isinstance(result, tuple):
        if len(result) == 3: result, media_type, ext = result
        elif len(result) == 2: result, ext = result; media_type = f"image/{ext}"

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], tool_name, "success", int((time.time() - t0) * 1000), len(result))
    return Response(content=result, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="lux_{tool_name}.{ext}"'})


@app.post("/api/tool/{tool_name}")
async def api_tool(tool_name: str, request: Request, file: UploadFile = File(...),
                  x_api_key: Optional[str] = Header(None)):
    if tool_name not in TOOLS: raise HTTPException(404, f"Unknown tool: {tool_name}")
    rec = await authenticate(x_api_key)
    params = dict(request.query_params)
    # Convert numeric params
    for k in ("quality", "radius", "angle", "width", "height", "brightness", "contrast", "saturation", "opacity", "thickness"):
        if k in params:
            try: params[k] = int(params[k])
            except: params.pop(k, None)
    return await _run_tool(tool_name, file, rec, params)


# ─── Admin ───
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(lux_admin: Optional[str] = Cookie(None)):
    if check_admin_session(lux_admin): return ADMIN_DASHBOARD_HTML
    return ADMIN_LOGIN_HTML


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password != ADMIN_PASSWORD: return RedirectResponse("/admin?error=1", status_code=303)
    token = new_admin_session()
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("lux_admin", token, max_age=ADMIN_SESSION_HOURS * 3600, httponly=True, samesite="lax")
    return resp


@app.post("/admin/logout")
async def admin_logout(lux_admin: Optional[str] = Cookie(None)):
    if lux_admin: _admin_sessions.pop(lux_admin, None)
    resp = RedirectResponse("/admin", status_code=303); resp.delete_cookie("lux_admin"); return resp


@app.get("/api/admin/stats")
async def admin_stats(lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    try:
        users = await dss.list("users"); keys = await dss.list("api_keys"); logs = await dss.list("usage_logs")
    except DSSError as e: raise HTTPException(503, str(e))
    return {
        "users": len(users), "keys_total": len(keys),
        "keys_active": sum(1 for k in keys if k.get("active")),
        "operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
        "banned": sum(1 for u in users if u.get("is_banned")),
        "total_credits": sum(int(u.get("credits", 0)) for u in users),
    }


@app.get("/api/admin/users")
async def admin_users(lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    try:
        users = await dss.list("users"); keys = await dss.list("api_keys")
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
            "id": uid, "ip": u.get("ip", "-"), "credits": int(u.get("credits", 0)),
            "usage_count": int(u.get("usage_count", 0)), "is_banned": bool(u.get("is_banned")),
            "created_at": u.get("created_at", ""), "prefix": k.get("prefix", "-"),
        })
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"items": items[:200], "total": len(items)}


@app.post("/api/admin/user/credits")
async def admin_user_credits(user_id: str = Form(...), amount: int = Form(...),
                            lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    if amount == 0: raise HTTPException(400, "Amount zero")
    if not await add_credits(user_id, amount): raise HTTPException(404, "User not found")
    return {"ok": True}


@app.post("/api/admin/user/ban")
async def admin_user_ban(user_id: str = Form(...), ban: bool = Form(True),
                        lux_admin: Optional[str] = Cookie(None)):
    require_admin(lux_admin)
    user = await get_user_by_id(user_id)
    if not user: raise HTTPException(404, "Not found")
    did = DSS.doc_id(user)
    if did is None: raise HTTPException(400, "Bad id")
    await dss.update("users", did, {"is_banned": ban})
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════
#  HTML - MAIN APP (with sidebar drawer + 9 pages)
# ═══════════════════════════════════════════════════════════════════
HTML_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#00e0ff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<title>Lux Studio VIP</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#05070f;--bg2:#0a0e1a;--surface:rgba(255,255,255,.035);--surface2:rgba(255,255,255,.08);--border:rgba(255,255,255,.08);--text:#e8ecf4;--muted:#8a94a8;--cyan:#00e0ff;--purple:#7c5cff;--pink:#ff2d95;--success:#00ffa3;--danger:#ff4d6d;--warn:#ffb020;--grad:linear-gradient(135deg,#00e0ff 0%,#7c5cff 50%,#ff2d95 100%);--shadow:0 20px 60px rgba(0,0,0,.5);--drawer-w:280px;--header-h:64px}
body.light{--bg:#f5f7fb;--bg2:#e8ecf4;--surface:#fff;--surface2:#f0f3f8;--border:rgba(0,0,0,.08);--text:#0a0e1a;--muted:#5a6478}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'Tajawal',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6;overflow-x:hidden;-webkit-tap-highlight-color:transparent;transition:background .3s,color .3s}
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(circle at 15% 20%,rgba(0,224,255,.10),transparent 35%),radial-gradient(circle at 85% 15%,rgba(124,92,255,.12),transparent 35%),radial-gradient(circle at 50% 90%,rgba(255,45,149,.08),transparent 40%)}
body.light::before{opacity:.35}
a{color:var(--cyan);text-decoration:none}
/* Header */
header{position:fixed;top:0;left:0;right:0;height:var(--header-h);z-index:100;backdrop-filter:blur(20px);background:rgba(5,7,15,.85);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 16px}
body.light header{background:rgba(245,247,251,.85)}
.hamburger{background:transparent;border:1px solid var(--border);color:var(--text);width:42px;height:42px;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1.3rem}
.hamburger:hover{background:var(--surface2)}
.header-title{font-weight:900;font-size:1.05rem;text-align:center;flex:1}
.header-title small{display:block;font-size:.65rem;color:var(--muted);font-weight:600}
.header-actions{display:flex;gap:8px}
.icon-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);width:42px;height:42px;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1rem;text-decoration:none}
.icon-btn:hover{background:rgba(255,255,255,.12)}
/* Drawer */
.drawer-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;opacity:0;pointer-events:none;transition:opacity .3s;backdrop-filter:blur(4px)}
.drawer-overlay.open{opacity:1;pointer-events:all}
.drawer{position:fixed;top:0;bottom:0;right:-320px;width:var(--drawer-w);background:var(--bg2);border-left:1px solid var(--border);z-index:201;transition:right .3s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;overflow-y:auto}
body.light .drawer{background:#fff}
.drawer.open{right:0}
.drawer-header{padding:24px 20px;border-bottom:1px solid var(--border);text-align:center}
.drawer-logo{font-size:1.5rem;font-weight:900}
.drawer-logo span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.drawer-user{margin-top:12px;padding:10px;background:var(--surface);border-radius:10px;font-size:.8rem}
.drawer-user .credits{font-size:1.2rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:'JetBrains Mono',monospace}
.drawer-nav{flex:1;padding:12px 0}
.nav-item{display:flex;align-items:center;gap:14px;padding:14px 20px;color:var(--text);cursor:pointer;border:none;background:transparent;width:100%;text-align:right;font-family:inherit;font-size:.95rem;font-weight:600;transition:background .2s;border-right:3px solid transparent}
.nav-item:hover{background:var(--surface)}
.nav-item.active{background:var(--surface2);border-right-color:var(--cyan);color:var(--cyan)}
.nav-item .ico{font-size:1.3rem;width:28px;text-align:center}
.nav-item .badge{margin-right:auto;background:rgba(0,255,163,.15);color:var(--success);padding:2px 8px;border-radius:20px;font-size:.7rem;font-weight:700}
.drawer-footer{padding:16px;border-top:1px solid var(--border);font-size:.75rem;color:var(--muted);text-align:center}
.drawer-footer a{color:var(--muted)}
.drawer-footer a:hover{color:var(--cyan)}
/* Main */
main{padding-top:calc(var(--header-h) + 16px);padding-bottom:40px;max-width:1000px;margin:0 auto;padding-left:16px;padding-right:16px;min-height:100vh}
.page{animation:fadeIn .25s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.page h1{font-size:1.5rem;font-weight:900;margin-bottom:4px}
.page .sub{color:var(--muted);font-size:.9rem;margin-bottom:24px}
.hidden{display:none !important}
/* Cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px;backdrop-filter:blur(10px)}
.card h3{font-size:1.1rem;margin-bottom:12px;font-weight:700}
/* Stats */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px;text-align:center}
.stat .num{font-size:1.8rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:'JetBrains Mono',monospace}
.stat .lbl{font-size:.75rem;color:var(--muted);margin-top:4px}
/* Buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:13px 22px;border-radius:12px;border:none;cursor:pointer;font-family:inherit;font-size:.95rem;font-weight:700;transition:all .2s;text-decoration:none}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--grad);color:#05070f;box-shadow:0 8px 30px rgba(124,92,255,.4)}
.btn-primary:hover:not(:disabled){transform:translateY(-2px)}
.btn-secondary{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-secondary:hover:not(:disabled){background:rgba(255,255,255,.12)}
.btn-block{width:100%}
.btn-lg{padding:16px 28px;font-size:1rem}
/* Upload zone */
.upload-zone{border:2px dashed var(--border);border-radius:16px;padding:44px 20px;text-align:center;cursor:pointer;transition:all .25s;background:var(--surface);margin-bottom:20px}
.upload-zone:hover,.upload-zone.dragover{border-color:var(--cyan);background:rgba(0,224,255,.05)}
.upload-zone svg{width:44px;height:44px;stroke:var(--cyan);margin-bottom:10px}
.upload-zone h3{font-size:1.05rem;margin-bottom:4px}
.upload-zone p{color:var(--muted);font-size:.85rem}
.upload-zone input{display:none}
/* Tools grid */
.tools-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px}
.tool-btn{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:16px 8px;border-radius:14px;cursor:pointer;font-family:inherit;font-weight:700;font-size:.8rem;transition:all .2s;display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}
.tool-btn:hover:not(:disabled){background:rgba(0,224,255,.1);border-color:var(--cyan);transform:translateY(-2px)}
.tool-btn .ic{font-size:1.6rem}
.tool-btn .lbl{line-height:1.2}
/* Preview */
.preview-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
@media(max-width:600px){.preview-row{grid-template-columns:1fr}}
.img-box{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:10px;text-align:center}
.img-box .label{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:8px}
.img-box img{max-width:100%;max-height:320px;border-radius:8px;display:block;margin:0 auto}
.checker{background-image:linear-gradient(45deg,#1a1f2e 25%,transparent 25%),linear-gradient(-45deg,#1a1f2e 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#1a1f2e 75%),linear-gradient(-45deg,transparent 75%,#1a1f2e 75%);background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0;background-color:#0d1220;border-radius:8px;padding:6px}
/* Loading */
.loading{display:flex;flex-direction:column;align-items:center;padding:30px;gap:12px}
.spinner{width:42px;height:42px;border-radius:50%;border:4px solid var(--border);border-top-color:var(--cyan);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading p{color:var(--muted);font-size:.9rem}
/* Forms */
.form-group{margin-bottom:16px}
.form-group label{display:block;margin-bottom:6px;font-weight:600;font-size:.85rem;color:var(--muted)}
.form-input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--bg2);color:var(--text);font-family:inherit;font-size:.9rem}
.form-input:focus{outline:none;border-color:var(--cyan)}
select.form-input{cursor:pointer}
/* API key display */
.key-display{display:flex;gap:8px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;align-items:center;font-family:'JetBrains Mono',monospace;font-size:.8rem;margin-bottom:10px;word-break:break-all}
.key-display span{flex:1;color:var(--cyan);text-align:left;direction:ltr;overflow:hidden}
.key-display button{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:8px;cursor:pointer;font-weight:700;font-size:.75rem}
/* History */
.history-item{display:flex;justify-content:space-between;align-items:center;padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;font-size:.85rem}
.history-item .action{font-weight:700}
.history-item .time{color:var(--muted);font-size:.75rem}
/* Toast */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(150%);background:var(--bg2);border:1px solid var(--border);padding:12px 22px;border-radius:12px;box-shadow:var(--shadow);z-index:999;font-weight:600;font-size:.9rem;transition:transform .3s;max-width:calc(100% - 32px);text-align:center}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}
/* Modal / Bottom sheet */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:300;display:none;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px)}
.modal-overlay.show{display:flex}
@media(max-width:600px){.modal-overlay{align-items:flex-end;padding:0}}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:20px;padding:24px;max-width:500px;width:100%;max-height:90vh;overflow-y:auto;animation:slideUp .3s ease}
@media(max-width:600px){.modal{border-radius:20px 20px 0 0;max-height:85vh}}
@keyframes slideUp{from{transform:translateY(40px);opacity:0}to{transform:translateY(0);opacity:1}}
.modal h3{margin-bottom:16px;font-size:1.1rem;font-weight:700}
.modal-actions{display:flex;gap:10px;margin-top:16px}
.modal-actions .btn{flex:1}
/* Install button */
.install-btn{display:none;position:fixed;bottom:16px;right:16px;z-index:150;padding:12px 20px;border-radius:30px;border:none;cursor:pointer;background:var(--grad);color:#05070f;font-family:inherit;font-weight:800;font-size:.85rem;box-shadow:0 10px 30px rgba(124,92,255,.5)}
.install-btn.show{display:flex;align-items:center;gap:6px}
/* Grid helpers */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:500px){.grid-2{grid-template-columns:1fr}}
.tag{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.7rem;font-weight:700;background:rgba(0,255,163,.15);color:var(--success)}
.tag-warn{background:rgba(255,176,32,.15);color:var(--warn)}
.tag-danger{background:rgba(255,77,109,.15);color:var(--danger)}
</style>
</head>
<body>

<header>
<button class="hamburger" onclick="openDrawer()" aria-label="القائمة">☰</button>
<div class="header-title">
<span id="header-title">Lux Studio</span>
<small id="header-sub">VIP Edition</small>
</div>
<div class="header-actions">
<a href="/admin" class="icon-btn" title="المدير">👑</a>
</div>
</header>

<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer">
<div class="drawer-header">
<div class="drawer-logo">Lux<span>.</span>VIP</div>
<div class="drawer-user">
<div style="font-size:.7rem;color:var(--muted);margin-bottom:4px">رصيدك</div>
<div class="credits" id="drawer-credits">-</div>
</div>
</div>
<nav class="drawer-nav">
<button class="nav-item active" data-page="home" onclick="navigate('home')"><span class="ico">🏠</span>الرئيسية</button>
<button class="nav-item" data-page="remove-bg" onclick="navigate('remove-bg')"><span class="ico">🖼️</span>إزالة الخلفية</button>
<button class="nav-item" data-page="enhance" onclick="navigate('enhance')"><span class="ico">✨</span>تحسين الجودة</button>
<button class="nav-item" data-page="designer" onclick="navigate('designer')"><span class="ico">🎨</span>أدوات المصمم</button>
<button class="nav-item" data-page="editor" onclick="navigate('editor')"><span class="ico">✂️</span>محرر متقدم</button>
<button class="nav-item" data-page="api" onclick="navigate('api')"><span class="ico">🔑</span>مفاتيح API</button>
<button class="nav-item" data-page="history" onclick="navigate('history')"><span class="ico">📜</span>سجلي</button>
<button class="nav-item" data-page="settings" onclick="navigate('settings')"><span class="ico">⚙️</span>الإعدادات</button>
<button class="nav-item" data-page="help" onclick="navigate('help')"><span class="ico">❓</span>المساعدة</button>
</nav>
<div class="drawer-footer">
<div>Lux Studio VIP v6</div>
<div style="margin-top:4px">© 2025 — Premium ✨</div>
</div>
</aside>

<button class="install-btn" id="install-btn" onclick="doInstall()">📱 تثبيت التطبيق</button>

<main>

<!-- ═══ HOME ═══ -->
<section class="page" id="page-home">
<h1>👋 أهلاً بك</h1>
<p class="sub">استوديو احترافي بـ 17 أداة مصمم</p>

<div id="welcome-section">
<div class="card" style="text-align:center;padding:40px 24px">
<div style="font-size:3rem;margin-bottom:16px">🚀</div>
<h3 style="margin-bottom:8px">احصل على مفتاحك المجاني</h3>
<p style="color:var(--muted);font-size:.9rem;margin-bottom:20px">30 عملية مجانية — بدون بطاقة بنكية</p>
<button class="btn btn-primary btn-lg" onclick="getFreeKey()">🎁 احصل على مفتاح الآن</button>
</div>
</div>

<div id="dashboard-section" class="hidden">
<div class="stats-grid">
<div class="stat"><div class="num" id="st-credits">-</div><div class="lbl">نقطة متبقية</div></div>
<div class="stat"><div class="num" id="st-ops">-</div><div class="lbl">إجمالي العمليات</div></div>
<div class="stat"><div class="num" id="st-success">-</div><div class="lbl">ناجحة</div></div>
</div>
<div class="card">
<h3>⚡ إجراءات سريعة</h3>
<div class="grid-2" style="margin-top:12px">
<button class="btn btn-primary" onclick="navigate('remove-bg')">🖼️ إزالة خلفية</button>
<button class="btn btn-secondary" onclick="navigate('enhance')">✨ تحسين صورة</button>
<button class="btn btn-secondary" onclick="navigate('designer')">🎨 أدوات المصمم</button>
<button class="btn btn-secondary" onclick="navigate('api')">🔑 إدارة API</button>
</div>
</div>
</div>
</section>

<!-- ═══ REMOVE BG ═══ -->
<section class="page hidden" id="page-remove-bg">
<h1>🖼️ إزالة الخلفية</h1>
<p class="sub">خلفية شفافة بدقة احترافية (U2Net AI)</p>
<div class="upload-zone" onclick="pickFile('remove-bg')">
<input type="file" accept="image/*" id="file-remove-bg">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>PNG · JPG · WEBP — حتى 15MB</p>
</div>
<div id="result-remove-bg" class="hidden"></div>
</section>

<!-- ═══ ENHANCE ═══ -->
<section class="page hidden" id="page-enhance">
<h1>✨ تحسين الجودة</h1>
<p class="sub">تكبير ×2 + Unsharp Mask + تحسين ألوان</p>
<div class="upload-zone" onclick="pickFile('enhance')">
<input type="file" accept="image/*" id="file-enhance">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>PNG · JPG · WEBP</p>
</div>
<div id="result-enhance" class="hidden"></div>
</section>

<!-- ═══ DESIGNER ═══ -->
<section class="page hidden" id="page-designer">
<h1>🎨 أدوات المصمم</h1>
<p class="sub">19 أداة احترافية جاهزة</p>
<div class="upload-zone" onclick="pickFile('designer')">
<input type="file" accept="image/*" id="file-designer">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>ستظهر جميع الأدوات</p>
</div>
<div id="designer-tools" class="hidden">
<div class="tools-grid">
<button class="tool-btn" onclick="runSimple('compress',{quality:70})"><span class="ic">📉</span><span class="lbl">ضغط</span></button>
<button class="tool-btn" onclick="runSimple('grayscale')"><span class="ic">⚫</span><span class="lbl">رمادي</span></button>
<button class="tool-btn" onclick="runSimple('sepia')"><span class="ic">🌅</span><span class="lbl">سيبيا</span></button>
<button class="tool-btn" onclick="runSimple('invert')"><span class="ic">🌗</span><span class="lbl">انعكاس</span></button>
<button class="tool-btn" onclick="runSimple('blur',{radius:5})"><span class="ic">💨</span><span class="lbl">بلور</span></button>
<button class="tool-btn" onclick="runSimple('sharpen')"><span class="ic">🔪</span><span class="lbl">حدّة</span></button>
<button class="tool-btn" onclick="runSimple('vintage')"><span class="ic">📷</span><span class="lbl">فينتاج</span></button>
<button class="tool-btn" onclick="runSimple('warm')"><span class="ic">☀️</span><span class="lbl">دافئ</span></button>
<button class="tool-btn" onclick="runSimple('cool')"><span class="ic">❄️</span><span class="lbl">بارد</span></button>
<button class="tool-btn" onclick="runSimple('rotate',{angle:90})"><span class="ic">🔃</span><span class="lbl">تدوير 90°</span></button>
<button class="tool-btn" onclick="runSimple('flip',{direction:'h'})"><span class="ic">↔️</span><span class="lbl">قلب أفقي</span></button>
<button class="tool-btn" onclick="runSimple('convert',{target:'png'})"><span class="ic">🔄</span><span class="lbl">PNG</span></button>
<button class="tool-btn" onclick="runSimple('convert',{target:'webp'})"><span class="ic">🔄</span><span class="lbl">WEBP</span></button>
<button class="tool-btn" onclick="openCropModal()"><span class="ic">✂️</span><span class="lbl">قص</span></button>
<button class="tool-btn" onclick="openResizeModal()"><span class="ic">📐</span><span class="lbl">تحجيم</span></button>
<button class="tool-btn" onclick="openWatermarkModal()"><span class="ic">💧</span><span class="lbl">علامة مائية</span></button>
<button class="tool-btn" onclick="openAdjustModal()"><span class="ic">🎚️</span><span class="lbl">ضبط</span></button>
<button class="tool-btn" onclick="openBgColorModal()"><span class="ic">🎨</span><span class="lbl">خلفية لون</span></button>
<button class="tool-btn" onclick="runSimple('round-corners',{radius:40})"><span class="ic">⭕</span><span class="lbl">زوايا</span></button>
<button class="tool-btn" onclick="runSimple('border',{thickness:20})"><span class="ic">🖼️</span><span class="lbl">إطار</span></button>
</div>
</div>
<div id="result-designer" class="hidden"></div>
</section>

<!-- ═══ EDITOR ═══ -->
<section class="page hidden" id="page-editor">
<h1>✂️ المحرر المتقدم</h1>
<p class="sub">تحكم كامل بالصورة</p>
<div class="upload-zone" onclick="pickFile('editor')">
<input type="file" accept="image/*" id="file-editor">
<svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
<h3>اختر صورة</h3><p>استخدم الأدوات المتقدمة</p>
</div>
<div id="editor-tools" class="hidden">
<div class="grid-2">
<button class="btn btn-secondary" onclick="openCropModal()">✂️ قص بنسبة</button>
<button class="btn btn-secondary" onclick="openResizeModal()">📐 تحجيم</button>
<button class="btn btn-secondary" onclick="openWatermarkModal()">💧 علامة مائية</button>
<button class="btn btn-secondary" onclick="openAdjustModal()">🎚️ ضبط</button>
<button class="btn btn-secondary" onclick="openBgColorModal()">🎨 خلفية لون</button>
<button class="btn btn-secondary" onclick="runSimple('border',{thickness:20})">🖼️ إطار</button>
</div>
</div>
<div id="result-editor" class="hidden"></div>
</section>

<!-- ═══ API KEYS ═══ -->
<section class="page hidden" id="page-api">
<h1>🔑 مفاتيح API</h1>
<p class="sub">اربط خدماتك ببوتات أو مواقع أخرى</p>

<div id="api-no-key" class="card">
<h3>لا يوجد مفتاح بعد</h3>
<p style="color:var(--muted);margin-bottom:16px">احصل على مفتاح مجاني فورًا</p>
<button class="btn btn-primary btn-block" onclick="getFreeKey()">🎁 إنشاء مفتاح</button>
</div>

<div id="api-with-key" class="hidden">
<div class="card" style="margin-bottom:16px">
<h3>🔑 مفتاحك الحالي</h3>
<div class="key-display">
<span id="api-key-masked">-</span>
<button onclick="toggleKeyVis()" id="btn-toggle-key">👁</button>
<button onclick="copyApiKey()">📋</button>
</div>
<p style="color:var(--warn);font-size:.78rem">⚠️ احتفظ به في مكان آمن</p>
</div>

<div class="card">
<h3>📖 طريقة الاستخدام</h3>
<div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px;font-family:'JetBrains Mono',monospace;font-size:.75rem;direction:ltr;text-align:left;overflow-x:auto;white-space:pre;color:#c9d1e0;margin-top:10px">
# Remove background
curl -X POST "YOUR_HOST/api/tool/remove-bg" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output result.png

# Enhance
curl -X POST "YOUR_HOST/api/tool/enhance" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output enhanced.jpg

# Info
curl "YOUR_HOST/api/me" -H "X-API-Key: YOUR_KEY"
</div>
</div>
</div>
</section>

<!-- ═══ HISTORY ═══ -->
<section class="page hidden" id="page-history">
<h1>📜 سجلي</h1>
<p class="sub">آخر 50 عملية</p>
<div class="card" id="history-container"><div class="loading"><div class="spinner"></div><p>جاري التحميل...</p></div></div>
</section>

<!-- ═══ SETTINGS ═══ -->
<section class="page hidden" id="page-settings">
<h1>⚙️ الإعدادات</h1>
<p class="sub">تخصيص التطبيق</p>
<div class="card" style="margin-bottom:12px">
<h3>🎨 المظهر</h3>
<div class="grid-2" style="margin-top:12px">
<button class="btn btn-secondary" onclick="setTheme('dark')">🌙 داكن</button>
<button class="btn btn-secondary" onclick="setTheme('light')">☀️ فاتح</button>
</div>
</div>
<div class="card" style="margin-bottom:12px">
<h3>🔐 الحساب</h3>
<p style="color:var(--muted);font-size:.85rem;margin-top:8px">مفتاحك محفوظ في هذا المتصفح. يمكنك مسحه (لن تتمكن من استرجاعه).</p>
<button class="btn btn-secondary btn-block" style="margin-top:12px" onclick="clearKey()">🗑️ مسح المفتاح من المتصفح</button>
</div>
<div class="card">
<h3>📱 التطبيق</h3>
<p style="color:var(--muted);font-size:.85rem;margin-top:8px">ثبّت التطبيق على شاشتك الرئيسية لتجربة APK كاملة</p>
<button class="btn btn-primary btn-block" style="margin-top:12px" onclick="doInstall()">📱 تثبيت التطبيق</button>
</div>
</section>

<!-- ═══ HELP ═══ -->
<section class="page hidden" id="page-help">
<h1>❓ المساعدة</h1>
<p class="sub">دليل سريع</p>
<div class="card" style="margin-bottom:12px">
<h3>🔑 كيف أحصل على مفتاح؟</h3>
<p style="color:var(--muted);font-size:.88rem;margin-top:8px">اذهب لصفحة <b>مفاتيح API</b> واضغط زر الإنشاء. المفتاح يظهر مرة واحدة فقط.</p>
</div>
<div class="card" style="margin-bottom:12px">
<h3>💳 ما هي النقاط؟</h3>
<p style="color:var(--muted);font-size:.88rem;margin-top:8px">كل عملية تستهلك نقطة واحدة. تحصل على 30 نقطة مجانًا. تواصل مع المدير لشحن المزيد.</p>
</div>
<div class="card" style="margin-bottom:12px">
<h3>⚡ لماذا أول عملية إزالة خلفية بطيئة؟</h3>
<p style="color:var(--muted);font-size:.88rem;margin-top:8px">لأن موديل الذكاء الاصطناعي يُحمَّل مرة واحدة عند أول استخدام. العمليات التالية ستكون سريعة.</p>
</div>
<div class="card">
<h3>🆘 الدعم</h3>
<p style="color:var(--muted);font-size:.88rem;margin-top:8px">تواصل مع مدير التطبيق عبر لوحة <b>/admin</b></p>
</div>
</section>

</main>

<!-- ═══ MODALS ═══ -->
<div class="modal-overlay" id="modal-crop">
<div class="modal">
<h3>✂️ قص بنسبة محددة</h3>
<div class="form-group">
<label>نسبة العرض:الارتفاع</label>
<select class="form-input" id="crop-ratio">
<option value="1:1">1:1 (مربع)</option>
<option value="4:3">4:3 (شاشة)</option>
<option value="16:9">16:9 (عريض)</option>
<option value="9:16">9:16 (ستوري)</option>
<option value="3:2">3:2 (كاميرا)</option>
</select>
</div>
<div class="modal-actions">
<button class="btn btn-secondary" onclick="closeModal('modal-crop')">إلغاء</button>
<button class="btn btn-primary" onclick="doCrop()">تطبيق</button>
</div>
</div>
</div>

<div class="modal-overlay" id="modal-resize">
<div class="modal">
<h3>📐 تحجيم الصورة</h3>
<div class="grid-2">
<div class="form-group"><label>العرض (px)</label><input type="number" class="form-input" id="resize-w" placeholder="اترك فارغًا"></div>
<div class="form-group"><label>الارتفاع (px)</label><input type="number" class="form-input" id="resize-h" placeholder="اترك فارغًا"></div>
</div>
<p style="color:var(--muted);font-size:.78rem;margin-bottom:12px">املأ واحدًا على الأقل — سيتم الحفاظ على النسبة</p>
<div class="modal-actions">
<button class="btn btn-secondary" onclick="closeModal('modal-resize')">إلغاء</button>
<button class="btn btn-primary" onclick="doResize()">تطبيق</button>
</div>
</div>
</div>

<div class="modal-overlay" id="modal-watermark">
<div class="modal">
<h3>💧 علامة مائية نصية</h3>
<div class="form-group"><label>النص</label><input type="text" class="form-input" id="wm-text" value="Lux Studio"></div>
<div class="grid-2">
<div class="form-group">
<label>الموضع</label>
<select class="form-input" id="wm-pos">
<option value="br">أسفل يمين</option>
<option value="bl">أسفل يسار</option>
<option value="tr">أعلى يمين</option>
<option value="tl">أعلى يسار</option>
<option value="c">الوسط</option>
</select>
</div>
<div class="form-group"><label>الشفافية (%)</label><input type="number" class="form-input" id="wm-opacity" value="60" min="10" max="100"></div>
</div>
<div class="modal-actions">
<button class="btn btn-secondary" onclick="closeModal('modal-watermark')">إلغاء</button>
<button class="btn btn-primary" onclick="doWatermark()">تطبيق</button>
</div>
</div>
</div>

<div class="modal-overlay" id="modal-adjust">
<div class="modal">
<h3>🎚️ ضبط الألوان</h3>
<div class="form-group"><label>السطوع: <span id="adj-b-val">0</span></label><input type="range" id="adj-b" min="-50" max="50" value="0" style="width:100%" oninput="document.getElementById('adj-b-val').textContent=this.value"></div>
<div class="form-group"><label>التباين: <span id="adj-c-val">0</span></label><input type="range" id="adj-c" min="-50" max="50" value="0" style="width:100%" oninput="document.getElementById('adj-c-val').textContent=this.value"></div>
<div class="form-group"><label>التشبع: <span id="adj-s-val">0</span></label><input type="range" id="adj-s" min="-50" max="50" value="0" style="width:100%" oninput="document.getElementById('adj-s-val').textContent=this.value"></div>
<div class="modal-actions">
<button class="btn btn-secondary" onclick="closeModal('modal-adjust')">إلغاء</button>
<button class="btn btn-primary" onclick="doAdjust()">تطبيق</button>
</div>
</div>
</div>

<div class="modal-overlay" id="modal-bgcolor">
<div class="modal">
<h3>🎨 استبدال الخلفية الشفافة بلون</h3>
<p style="color:var(--muted);font-size:.85rem;margin-bottom:12px">استخدم بعد إزالة الخلفية</p>
<div class="form-group"><label>اللون</label><input type="color" class="form-input" id="bgcolor-val" value="#ffffff" style="height:50px;padding:4px"></div>
<div class="modal-actions">
<button class="btn btn-secondary" onclick="closeModal('modal-bgcolor')">إلغاء</button>
<button class="btn btn-primary" onclick="doBgColor()">تطبيق</button>
</div>
</div>
</div>

<div class="toast" id="toast"></div>

<script>
// ═══ STATE ═══
const state = {
  key: localStorage.getItem('lux_api_key') || null,
  currentPage: 'home',
  currentFile: null,
  currentSource: null, // 'designer' / 'editor' / 'remove-bg' / 'enhance'
  theme: localStorage.getItem('lux_theme') || 'dark',
  keyVisible: false,
  deferredPrompt: null,
};

const PAGES = {
  home: {title: 'الرئيسية', sub: 'Lux Studio VIP'},
  'remove-bg': {title: 'إزالة الخلفية', sub: 'شفافية احترافية'},
  enhance: {title: 'تحسين الجودة', sub: 'تكبير ×2'},
  designer: {title: 'أدوات المصمم', sub: '19 أداة'},
  editor: {title: 'المحرر المتقدم', sub: 'تحكم كامل'},
  api: {title: 'مفاتيح API', sub: 'اربط خدماتك'},
  history: {title: 'سجلي', sub: 'العمليات الأخيرة'},
  settings: {title: 'الإعدادات', sub: 'تخصيص'},
  help: {title: 'المساعدة', sub: 'دليل سريع'},
};

// ═══ NAVIGATION ═══
function navigate(page) {
  if (!PAGES[page]) page = 'home';
  state.currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  const target = document.getElementById('page-' + page);
  if (target) { target.classList.remove('hidden'); }
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });
  document.getElementById('header-title').textContent = PAGES[page].title;
  document.getElementById('header-sub').textContent = PAGES[page].sub;
  closeDrawer();
  window.scrollTo({top:0, behavior:'smooth'});
  if (page === 'history') loadHistory();
  if (page === 'api') refreshUser();
  history.replaceState(null, '', '#/' + page);
}

function openDrawer() {
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
}
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
}

// ═══ TOAST ═══
function toast(msg, type='success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = 'toast ' + type, 3500);
}

// ═══ MODALS ═══
function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }
document.querySelectorAll('.modal-overlay').forEach(m => {
  m.addEventListener('click', e => { if (e.target === m) m.classList.remove('show'); });
});

// ═══ THEME ═══
function setTheme(t) {
  state.theme = t;
  localStorage.setItem('lux_theme', t);
  applyTheme();
  toast(t === 'light' ? '☀️ مظهر فاتح' : '🌙 مظهر داكن');
}
function applyTheme() { document.body.classList.toggle('light', state.theme === 'light'); }

// ═══ KEY MANAGEMENT ═══
async function getFreeKey() {
  try {
    const r = await fetch('/api/keys/new', {method: 'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'فشل');
    state.key = d.api_key;
    localStorage.setItem('lux_api_key', d.api_key);
    toast('✅ تم إنشاء المفتاح');
    await refreshUser();
    navigate('home');
    // Show key in a modal
    showKeyModal(d.api_key);
  } catch (e) {
    toast('❌ ' + e.message, 'error');
  }
}

function showKeyModal(key) {
  const m = document.createElement('div');
  m.className = 'modal-overlay show';
  m.innerHTML = `<div class="modal">
    <h3>🎉 مفتاحك الجديد</h3>
    <p style="color:var(--warn);font-size:.85rem;margin-bottom:12px">⚠️ احفظه الآن — لن يظهر مرة أخرى</p>
    <div class="key-display"><span>${key}</span><button onclick="navigator.clipboard.writeText('${key}');toast('📋 تم النسخ')">📋</button></div>
    <div class="modal-actions"><button class="btn btn-primary" onclick="this.closest('.modal-overlay').remove()">حفظت المفتاح ✅</button></div>
  </div>`;
  document.body.appendChild(m);
}

function maskKey(k) { return k ? k.slice(0,10) + '••••••' + k.slice(-4) : '-'; }
function toggleKeyVis() {
  state.keyVisible = !state.keyVisible;
  document.getElementById('api-key-masked').textContent = state.keyVisible ? state.key : maskKey(state.key);
  document.getElementById('btn-toggle-key').textContent = state.keyVisible ? '🙈' : '👁';
}
function copyApiKey() {
  if (state.key) navigator.clipboard.writeText(state.key).then(() => toast('📋 تم النسخ'));
}

async function refreshUser() {
  if (!state.key) {
    document.getElementById('welcome-section').classList.remove('hidden');
    document.getElementById('dashboard-section').classList.add('hidden');
    document.getElementById('api-no-key').classList.remove('hidden');
    document.getElementById('api-with-key').classList.add('hidden');
    return;
  }
  try {
    const r = await fetch('/api/me', {headers:{'X-API-Key': state.key}});
    if (r.status === 401) {
      localStorage.removeItem('lux_api_key');
      state.key = null;
      toast('⚠️ مفتاحك غير صالح', 'error');
      return refreshUser();
    }
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('welcome-section').classList.add('hidden');
    document.getElementById('dashboard-section').classList.remove('hidden');
    document.getElementById('api-no-key').classList.add('hidden');
    document.getElementById('api-with-key').classList.remove('hidden');
    document.getElementById('st-credits').textContent = d.credits;
    document.getElementById('st-ops').textContent = d.total_operations;
    document.getElementById('st-success').textContent = d.successful;
    document.getElementById('drawer-credits').textContent = d.credits;
    document.getElementById('api-key-masked').textContent = state.keyVisible ? state.key : maskKey(state.key);
  } catch (e) {}
}

function clearKey() {
  if (!confirm('هل تريد مسح المفتاح؟ لن تتمكن من استرجاعه.')) return;
  localStorage.removeItem('lux_api_key');
  state.key = null;
  toast('🗑️ تم المسح');
  navigate('home');
  refreshUser();
}

// ═══ FILE PICKING ═══
function pickFile(source) {
  state.currentSource = source;
  document.getElementById('file-' + source).click();
}

['remove-bg', 'enhance', 'designer', 'editor'].forEach(src => {
  document.getElementById('file-' + src).addEventListener('change', e => {
    if (e.target.files.length) handleFile(e.target.files[0], src);
    e.target.value = '';
  });
});

function handleFile(file, source) {
  if (!file.type.startsWith('image/')) return toast('❌ ليس صورة', 'error');
  if (file.size > 15 * 1024 * 1024) return toast('❌ أكبر من 15MB', 'error');
  state.currentFile = file;
  state.currentSource = source;

  if (source === 'designer') {
    document.getElementById('designer-tools').classList.remove('hidden');
    document.getElementById('result-designer').classList.add('hidden');
    toast('📸 اختر أداة');
  } else if (source === 'editor') {
    document.getElementById('editor-tools').classList.remove('hidden');
    document.getElementById('result-editor').classList.add('hidden');
    toast('📸 اختر أداة');
  } else if (source === 'remove-bg') {
    executeTool('remove-bg', {}, 'result-remove-bg');
  } else if (source === 'enhance') {
    executeTool('enhance', {}, 'result-enhance');
  }
}

// ═══ EXECUTE TOOL ═══
async function executeTool(toolName, params, resultId) {
  if (!state.currentFile) return toast('❌ اختر صورة أولاً', 'error');
  if (!state.key) return toast('❌ مفتاح مفقود — أنشئ واحدًا من صفحة API', 'error');

  const container = document.getElementById(resultId);
  container.classList.remove('hidden');
  container.innerHTML = `<div class="card"><div class="loading"><div class="spinner"></div><p>⏳ جاري المعالجة...</p></div></div>`;

  const form = new FormData();
  form.append('file', state.currentFile);
  const qs = new URLSearchParams(params).toString();
  const url = '/api/tool/' + toolName + (qs ? '?' + qs : '');

  const t0 = performance.now();
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'X-API-Key': state.key},
      body: form,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `خطأ ${r.status}`);
    }
    const blob = await r.blob();
    const objUrl = URL.createObjectURL(blob);
    const ext = blob.type === 'image/png' ? 'png' : blob.type === 'image/webp' ? 'webp' : 'jpg';
    const reader = new FileReader();
    reader.onload = e => {
      container.innerHTML = `
        <div class="preview-row">
          <div class="img-box"><div class="label">الأصلية</div><img src="${e.target.result}" alt=""></div>
          <div class="img-box"><div class="label">النتيجة</div><div class="${toolName === 'remove-bg' ? 'checker' : ''}"><img src="${objUrl}" alt=""></div></div>
        </div>
        <div class="grid-2" style="margin-top:12px">
          <a href="${objUrl}" download="lux_${toolName}.${ext}" class="btn btn-primary">⬇️ تحميل</a>
          <button class="btn btn-secondary" onclick="resetPage('${state.currentSource}')">🔄 صورة جديدة</button>
        </div>`;
    };
    reader.readAsDataURL(state.currentFile);
    toast(`✅ تمت في ${Math.round(performance.now() - t0)}ms`);
    await refreshUser();
  } catch (e) {
    container.innerHTML = `<div class="card"><div style="color:var(--danger);text-align:center;padding:20px">❌ ${e.message}</div></div>`;
    toast('❌ ' + e.message, 'error');
  }
}

function runSimple(toolName, params = {}) {
  if (!state.currentFile) return toast('❌ اختر صورة', 'error');
  const resultId = state.currentSource === 'designer' ? 'result-designer' : 'result-editor';
  executeTool(toolName, params, resultId);
}

function resetPage(source) {
  state.currentFile = null;
  document.getElementById('file-' + source).value = '';
  if (source === 'designer') {
    document.getElementById('designer-tools').classList.add('hidden');
    document.getElementById('result-designer').classList.add('hidden');
  } else if (source === 'editor') {
    document.getElementById('editor-tools').classList.add('hidden');
    document.getElementById('result-editor').classList.add('hidden');
  } else if (source === 'remove-bg') {
    document.getElementById('result-remove-bg').classList.add('hidden');
  } else if (source === 'enhance') {
    document.getElementById('result-enhance').classList.add('hidden');
  }
}

// ═══ MODAL ACTIONS ═══
function openCropModal() { if (!state.currentFile) return toast('❌ اختر صورة', 'error'); openModal('modal-crop'); }
function openResizeModal() { if (!state.currentFile) return toast('❌ اختر صورة', 'error'); openModal('modal-resize'); }
function openWatermarkModal() { if (!state.currentFile) return toast('❌ اختر صورة', 'error'); openModal('modal-watermark'); }
function openAdjustModal() { if (!state.currentFile) return toast('❌ اختر صورة', 'error'); openModal('modal-adjust'); }
function openBgColorModal() { if (!state.currentFile) return toast('❌ اختر صورة', 'error'); openModal('modal-bgcolor'); }

function doCrop() {
  const ratio = document.getElementById('crop-ratio').value;
  closeModal('modal-crop');
  runSimple('crop', {ratio});
}
function doResize() {
  const w = document.getElementById('resize-w').value;
  const h = document.getElementById('resize-h').value;
  if (!w && !h) return toast('❌ أدخل عرضًا أو ارتفاعًا', 'error');
  closeModal('modal-resize');
  const p = {};
  if (w) p.width = parseInt(w);
  if (h) p.height = parseInt(h);
  runSimple('resize', p);
}
function doWatermark() {
  const text = document.getElementById('wm-text').value || 'Lux Studio';
  const pos = document.getElementById('wm-pos').value;
  const opacity = document.getElementById('wm-opacity').value;
  closeModal('modal-watermark');
  runSimple('watermark', {text, position: pos, opacity: parseInt(opacity)});
}
function doAdjust() {
  const b = parseInt(document.getElementById('adj-b').value);
  const c = parseInt(document.getElementById('adj-c').value);
  const s = parseInt(document.getElementById('adj-s').value);
  if (b === 0 && c === 0 && s === 0) return toast('⚠️ لا تغيير', 'error');
  closeModal('modal-adjust');
  runSimple('adjust', {brightness: b, contrast: c, saturation: s});
}
function doBgColor() {
  const color = document.getElementById('bgcolor-val').value;
  closeModal('modal-bgcolor');
  runSimple('bg-color', {color});
}

// ═══ HISTORY ═══
async function loadHistory() {
  const el = document.getElementById('history-container');
  if (!state.key) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">لا يوجد مفتاح — أنشئ واحدًا أولاً</div>';
    return;
  }
  el.innerHTML = '<div class="loading"><div class="spinner"></div><p>جاري التحميل...</p></div>';
  try {
    const r = await fetch('/api/history', {headers: {'X-API-Key': state.key}});
    if (!r.ok) throw new Error();
    const d = await r.json();
    if (!d.items || !d.items.length) {
      el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">لا يوجد سجل بعد</div>';
      return;
    }
    el.innerHTML = d.items.slice(0, 30).map(x => `
      <div class="history-item">
        <span class="action">${x.action || '?'}</span>
        <span style="color:${x.status === 'success' ? 'var(--success)' : 'var(--danger)'}">${x.status === 'success' ? '✅' : '❌'}</span>
        <span class="time">${(x.created_at || '').slice(0,16).replace('T', ' ')}</span>
      </div>
    `).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:var(--danger);text-align:center;padding:20px">فشل التحميل</div>';
  }
}

// ═══ HEALTH ═══
async function checkHealth() {
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    // silent
  } catch (e) {}
}

// ═══ PWA INSTALL ═══
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  state.deferredPrompt = e;
  document.getElementById('install-btn').classList.add('show');
});
function doInstall() {
  if (!state.deferredPrompt) {
    toast('افتح الصفحة في Chrome على جوالك', 'error');
    return;
  }
  state.deferredPrompt.prompt();
  state.deferredPrompt.userChoice.then(() => {
    state.deferredPrompt = null;
    document.getElementById('install-btn').classList.remove('show');
  });
}

// ═══ INIT ═══
window.addEventListener('DOMContentLoaded', () => {
  applyTheme();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  const page = location.hash.replace('#/', '') || 'home';
  navigate(page);
  refreshUser();
  checkHealth();
  setInterval(checkHealth, 60000);
});

window.addEventListener('hashchange', () => {
  const page = location.hash.replace('#/', '') || 'home';
  if (page !== state.currentPage) navigate(page);
});
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
#  ADMIN LOGIN HTML
# ═══════════════════════════════════════════════════════════════════
ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin — Lux VIP</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#05070f;color:#e8ecf4;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background-image:radial-gradient(circle at 20% 20%,rgba(0,224,255,.12),transparent 40%),radial-gradient(circle at 80% 30%,rgba(124,92,255,.14),transparent 40%)}
.card{background:rgba(255,255,255,.04);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:48px 40px;max-width:420px;width:100%;box-shadow:0 30px 80px rgba(0,0,0,.6);text-align:center}
.icon{font-size:3.5rem;margin-bottom:16px}
h1{font-size:1.6rem;margin-bottom:8px;background:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#8a94a8;font-size:.9rem;margin-bottom:32px}
label{display:block;text-align:right;font-weight:600;margin-bottom:8px;font-size:.85rem;color:#8a94a8}
input{width:100%;padding:14px 16px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:#05070f;color:#e8ecf4;font-size:1rem;margin-bottom:20px}
input:focus{outline:none;border-color:#00e0ff}
button{width:100%;padding:14px;border:none;border-radius:12px;background:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95);color:#05070f;font-weight:800;cursor:pointer;font-size:1rem}
.back{display:inline-block;margin-top:24px;color:#8a94a8;text-decoration:none;font-size:.85rem}
</style></head><body>
<div class="card">
<div class="icon">👑</div><h1>لوحة المدير</h1><p class="sub">Lux Studio VIP — Admin</p>
<form method="post" action="/admin/login">
<label>كلمة المرور</label>
<input type="password" name="password" required autofocus placeholder="••••••••">
<button>🔓 دخول</button>
</form>
<a href="/" class="back">← العودة للتطبيق</a>
</div></body></html>"""


# ═══════════════════════════════════════════════════════════════════
#  ADMIN DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════
ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin — Lux VIP</title>
<style>
:root{--bg:#05070f;--bg2:#0a0e1a;--surface:rgba(255,255,255,.04);--border:rgba(255,255,255,.08);--text:#e8ecf4;--muted:#8a94a8;--cyan:#00e0ff;--success:#00ffa3;--danger:#ff4d6d;--grad:linear-gradient(135deg,#00e0ff,#7c5cff,#ff2d95)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(circle at 15% 20%,rgba(0,224,255,.08),transparent 35%),radial-gradient(circle at 85% 15%,rgba(124,92,255,.10),transparent 35%)}
header{background:rgba(5,7,15,.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10}
.logo{font-weight:900;font-size:1.2rem}.logo span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.logout{background:rgba(255,77,109,.15);border:1px solid rgba(255,77,109,.3);color:var(--danger);padding:8px 16px;border-radius:8px;cursor:pointer;font-weight:700;font-size:.85rem}
main{max-width:1200px;margin:0 auto;padding:24px}
h1{font-size:1.5rem;margin-bottom:20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center}
.stat .num{font-size:1.6rem;font-weight:900;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-family:monospace}
.stat .lbl{color:var(--muted);font-size:.75rem;margin-top:2px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.82rem;min-width:600px}
th{text-align:right;padding:10px 8px;color:var(--muted);font-size:.72rem;text-transform:uppercase;border-bottom:1px solid var(--border)}
td{padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.03)}
.mono{font-family:monospace;font-size:.75rem;color:var(--cyan)}
.btn{padding:5px 10px;border:none;border-radius:6px;font-size:.72rem;font-weight:700;cursor:pointer;margin:2px}
.btn-add{background:rgba(0,255,163,.15);color:var(--success);border:1px solid rgba(0,255,163,.3)}
.btn-ban{background:rgba(255,176,32,.15);color:#ffb020;border:1px solid rgba(255,176,32,.3)}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(150%);background:var(--bg2);border:1px solid var(--border);padding:12px 22px;border-radius:12px;z-index:99;font-weight:600;transition:transform .3s}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
</style></head><body>
<header><div class="logo">👑 Lux <span>VIP</span> Admin</div><form method="post" action="/admin/logout" style="margin:0"><button class="logout">🚪 خروج</button></form></header>
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
<div class="card" id="users-table">جاري التحميل...</div>
</main>
<div class="toast" id="toast"></div>
<script>
function toast(m, t='success'){const e=document.getElementById('toast');e.textContent=m;e.className='toast show '+t;clearTimeout(e._t);e._t=setTimeout(()=>e.className='toast '+t,3000)}
async function load(){
  try{
    const [s,u]=await Promise.all([fetch('/api/admin/stats').then(r=>r.json()),fetch('/api/admin/users').then(r=>r.json())]);
    document.getElementById('s-users').textContent=s.users;
    document.getElementById('s-keys').textContent=s.keys_active;
    document.getElementById('s-ops').textContent=s.operations;
    document.getElementById('s-success').textContent=s.successful;
    document.getElementById('s-banned').textContent=s.banned;
    document.getElementById('s-credits').textContent=s.total_credits;
    const el=document.getElementById('users-table');
    if(!u.items.length){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--muted)">لا يوجد مستخدمون</div>';return}
    el.innerHTML=`<table><thead><tr><th>المعرف</th><th>IP</th><th>النقاط</th><th>الاستخدام</th><th>البادئة</th><th>الحالة</th><th>إجراءات</th></tr></thead><tbody>${u.items.map(x=>`<tr>
      <td class="mono">${x.id.slice(0,10)}</td>
      <td style="color:var(--muted);font-size:.7rem">${x.ip}</td>
      <td><strong style="color:var(--cyan)">${x.credits}</strong></td>
      <td>${x.usage_count}</td>
      <td class="mono">${x.prefix}</td>
      <td>${x.is_banned?'<span style="color:var(--danger)">محظور</span>':'<span style="color:var(--success)">نشط</span>'}</td>
      <td>
        <button class="btn btn-add" onclick="addCredits('${x.id}')">+ نقاط</button>
        <button class="btn btn-ban" onclick="toggleBan('${x.id}',${!x.is_banned})">${x.is_banned?'فك حظر':'حظر'}</button>
      </td>
    </tr>`).join('')}</tbody></table>`;
  }catch(e){toast('فشل التحميل','error')}
}
async function addCredits(id){
  const amt=prompt('عدد النقاط (سالب للخصم):');
  if(!amt)return;
  const f=new FormData();f.append('user_id',id);f.append('amount',amt);
  const r=await fetch('/api/admin/user/credits',{method:'POST',body:f});
  if(r.ok){toast('✅ تم');load()}else toast('❌ فشل','error');
}
async function toggleBan(id,ban){
  if(!confirm(ban?'حظر؟':'فك الحظر؟'))return;
  const f=new FormData();f.append('user_id',id);f.append('ban',ban?'true':'false');
  const r=await fetch('/api/admin/user/ban',{method:'POST',body:f});
  if(r.ok){toast('✅ تم');load()}else toast('❌ فشل','error');
}
load();setInterval(load,30000);
</script></body></html>"""


# ═══════════════════════════════════════════════════════════════════
#  ASSETS
# ═══════════════════════════════════════════════════════════════════
SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#00e0ff"/><stop offset="0.5" stop-color="#7c5cff"/><stop offset="1" stop-color="#ff2d95"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="#05070f"/><circle cx="256" cy="256" r="140" fill="none" stroke="url(#g)" stroke-width="20"/><path d="M200 320 L256 200 L312 320 Z" fill="url(#g)"/><circle cx="256" cy="180" r="22" fill="url(#g)"/></svg>"""

SW_JS = """self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;if(e.request.url.includes('/api/'))return;if(e.request.url.endsWith('/ping'))return;e.respondWith(caches.open('lux-v6').then(c=>fetch(e.request).then(r=>{if(r.ok&&r.type==='basic')c.put(e.request,r.clone());return r}).catch(()=>c.match(e.request))))});"""


# ═══════════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"👑 Lux Studio VIP v6.0 → port {PORT}", flush=True)
    print(f"📦 DSS: {DSS_BASE_URL}", flush=True)
    print(f"🔐 Admin: /admin (password: {ADMIN_PASSWORD})", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
