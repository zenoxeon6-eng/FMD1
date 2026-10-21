"""
╔══════════════════════════════════════════════════════════════════╗
║   🌐 Lux Image Studio — تطبيق ويب احترافي (ملف واحد)              ║
║   إزالة خلفية | تحسين | ضغط | تحويل صيغة | تدرج رمادي | API Keys  ║
║   ملف واحد — مفتاح DSS مضمّن — PWA قابل للتثبيت كتطبيق            ║
║   نسخة متوافقة مع Render (Python 3.12) + نبضة كل 10 دقائق         ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════
#  [1] الاستيرادات
# ═══════════════════════════════════════════════════════════════════
import os
import io
import time
import secrets
import hashlib
import logging
import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Optional

import httpx
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from fastapi import FastAPI, File, UploadFile, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ═══════════════════════════════════════════════════════════════════
#  [2] الإعدادات (المفتاح مضمّن هنا)
# ═══════════════════════════════════════════════════════════════════
DSS_BASE_URL = "https://kali-imvx.onrender.com"
DSS_API_KEY  = "dss_SSiPIYB4EpWwVmfygQ1LTF2KymOjd-qxAQ4U3Q"

FREE_CREDITS   = 20
MAX_FILE_MB    = 15
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
PORT           = int(os.getenv("PORT", "8000"))

# rembg (اختياري — إذا فشل، التحسين والضغط يبقيان يعملان)
try:
    from rembg import remove as rembg_remove
    HAS_REMBG = True
except Exception as _e:
    HAS_REMBG = False
    print(f"⚠️ rembg غير متاح ({_e}) — إزالة الخلفية معطلة، الباقي يعمل.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("lux")


# ═══════════════════════════════════════════════════════════════════
#  [3] عميل قاعدة البيانات DSS
# ═══════════════════════════════════════════════════════════════════
class DSSError(Exception):
    pass


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
            if "404" in str(e):
                return []
            raise
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            for k in ("items", "data", "results", "records", "posts", "rows", "documents", "docs", "list"):
                if isinstance(res.get(k), list):
                    return res[k]
            if "id" in res or "_id" in res:
                return [res]
        return []

    async def create(self, collection: str, data: dict) -> dict:
        res = await self._req("POST", f"/api/db/{collection}", json=data)
        if isinstance(res, dict):
            for key in ("id", "_id", "uuid", "key", "ID"):
                if key in res:
                    return {**data, **res}
            if isinstance(res.get("data"), dict):
                return {**data, **res["data"]}
        return {**data, "_raw": res}

    async def update(self, collection: str, doc_id, data: dict) -> dict:
        for method in ("PATCH", "PUT"):
            try:
                return await self._req(method, f"/api/db/{collection}/{doc_id}", json=data)
            except DSSError:
                continue
        try:
            return await self._req("POST", f"/api/db/{collection}", json={**data, "id": doc_id})
        except DSSError as e:
            log.warning(f"update fallback failed: {e}")
            return {}

    @staticmethod
    def doc_id(doc: dict):
        if not isinstance(doc, dict):
            return None
        for k in ("id", "_id", "uuid", "key", "ID"):
            if k in doc and doc[k] is not None:
                return doc[k]
        return None


dss = DSS()


# ═══════════════════════════════════════════════════════════════════
#  [4] المصادقة والمستخدمين والمفاتيح
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
            if u.get("ip") != ip:
                continue
            try:
                ts = datetime.fromisoformat(
                    str(u.get("created_at", "")).replace("Z", "+00:00")
                ).timestamp()
                if ts >= cutoff:
                    count += 1
            except Exception:
                pass
    except DSSError:
        pass
    return count


async def create_free_key(ip: str) -> dict:
    user_id = secrets.token_urlsafe(12)
    user_doc = {
        "id": user_id,
        "ip": ip,
        "credits": FREE_CREDITS,
        "usage_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await dss.create("users", user_doc)

    plain = f"lux_{secrets.token_urlsafe(32)}"
    prefix = plain[:12]
    key_doc = {
        "id": secrets.token_urlsafe(12),
        "user_id": user_id,
        "hash": sha256(plain),
        "prefix": prefix,
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await dss.create("api_keys", key_doc)
    return {
        "api_key": plain,
        "prefix": prefix,
        "credits": FREE_CREDITS,
        "user_id": user_id,
    }


async def deduct_credit(user_id: str) -> bool:
    user = await get_user_by_id(user_id)
    if not user:
        return False
    credits = int(user.get("credits", 0))
    if credits < 1:
        return False
    did = DSS.doc_id(user)
    if did is None:
        return False
    try:
        await dss.update("users", did, {
            "credits": credits - 1,
            "usage_count": int(user.get("usage_count", 0)) + 1,
        })
        return True
    except Exception as e:
        log.warning(f"deduct_credit: {e}")
        return False


async def log_usage(user_id: str, action: str, status: str, duration_ms: int):
    try:
        await dss.create("usage_logs", {
            "id": secrets.token_urlsafe(12),
            "user_id": user_id,
            "action": action,
            "status": status,
            "duration_ms": duration_ms,
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
    return rec


# ═══════════════════════════════════════════════════════════════════
#  [5] معالجة الصور
# ═══════════════════════════════════════════════════════════════════
def _open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as e:
        raise ValueError(f"الملف ليس صورة صالحة: {e}")


def process_remove_bg(data: bytes) -> bytes:
    if not HAS_REMBG:
        raise RuntimeError("ميزة إزالة الخلفية غير متوفرة (rembg غير مثبت).")
    return rembg_remove(data)


def process_enhance(data: bytes) -> tuple:
    img = _open_image(data)
    has_alpha = (
        img.mode in ("RGBA", "LA")
        or (img.mode == "P" and "transparency" in img.info)
    )
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

    out = io.BytesIO()
    if has_alpha:
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), "png"
    img.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue(), "jpg"


def process_compress(data: bytes, quality: int = 75) -> bytes:
    img = _open_image(data)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if img.mode in ("P", "LA") else "RGB")
    out = io.BytesIO()
    if img.mode == "RGBA":
        img.save(out, format="PNG", optimize=True)
    else:
        img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def process_convert(data: bytes, target: str) -> tuple:
    img = _open_image(data)
    target = target.lower()
    if target not in ("jpg", "jpeg", "png", "webp"):
        raise ValueError(f"صيغة غير مدعومة: {target}")
    if target in ("jpg", "jpeg"):
        if img.mode != "RGB":
            img = img.convert("RGB")
        fmt, mime, ext = "JPEG", "image/jpeg", "jpg"
        kwargs = {"quality": 92, "optimize": True}
    elif target == "png":
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        fmt, mime, ext = "PNG", "image/png", "png"
        kwargs = {"optimize": True}
    else:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        fmt, mime, ext = "WEBP", "image/webp", "webp"
        kwargs = {"quality": 90, "method": 6}
    out = io.BytesIO()
    img.save(out, format=fmt, **kwargs)
    return out.getvalue(), mime, ext


def process_grayscale(data: bytes) -> bytes:
    img = _open_image(data)
    if img.mode in ("RGBA", "LA"):
        alpha = img.split()[-1]
        gray = ImageOps.grayscale(img.convert("RGB")).convert("RGBA")
        gray.putalpha(alpha)
        img = gray
    else:
        img = ImageOps.grayscale(img.convert("RGB"))
    out = io.BytesIO()
    if img.mode == "RGBA":
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    img.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()


# ═══════════════════════════════════════════════════════════════════
#  [6] نبضة داخلية (Self-ping) — تعمل فقط عندما يكون الخادم مستيقظًا
# ═══════════════════════════════════════════════════════════════════
async def _keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        log.info("ℹ️ RENDER_EXTERNAL_URL غير محدد — self-ping معطل (طبيعي محليًا)")
        return
    ping_url = f"{url}/ping"
    log.info(f"❤️ Self-ping كل 10 دقائق → {ping_url}")
    while True:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(ping_url)
                log.info(f"💓 ping {r.status_code}")
        except Exception as e:
            log.warning(f"💔 ping failed: {e}")


@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_keep_alive())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ═══════════════════════════════════════════════════════════════════
#  [7] تطبيق FastAPI
# ═══════════════════════════════════════════════════════════════════
app = FastAPI(title="Lux Image Studio", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────── نقاط ثابتة (لا تلمس قاعدة البيانات) ───────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/ping")
async def ping():
    """نبضة خفيفة جدًا — لا تلمس قاعدة البيانات إطلاقًا."""
    return {"status": "alive", "ts": time.time()}


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Lux Image Studio",
        "short_name": "Lux Studio",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#05070f",
        "theme_color": "#00e0ff",
        "description": "استوديو احترافي لإزالة الخلفيات وتحسين الصور",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
    })


@app.get("/icon.svg")
async def icon():
    return Response(content=SVG_ICON, media_type="image/svg+xml")


@app.get("/sw.js")
async def service_worker():
    return Response(content=SW_JS, media_type="application/javascript")


# ─────────── إدارة المفاتيح ───────────
@app.post("/api/keys/new")
async def api_new_key(request: Request):
    ip = request.client.host if request.client else "unknown"
    try:
        if await ip_key_count(ip) >= 3:
            raise HTTPException(429, "تم إنشاء مفاتيح كثيرة من هذا العنوان. حاول بعد 24 ساعة.")
        result = await create_free_key(ip)
    except HTTPException:
        raise
    except DSSError as e:
        log.exception("create key failed")
        raise HTTPException(503, f"فشل الاتصال بقاعدة البيانات: {e}")
    except Exception as e:
        log.exception("create key failed")
        raise HTTPException(500, f"خطأ: {e}")
    return {**result, "message": "احفظ هذا المفتاح الآن — لن يظهر مرة أخرى"}


@app.get("/api/me")
async def api_me(x_api_key: Optional[str] = Header(None)):
    rec = await authenticate(x_api_key)
    user = await get_user_by_id(rec.get("user_id"))
    if not user:
        raise HTTPException(404, "المستخدم غير موجود")
    try:
        logs = [l for l in await dss.list("usage_logs") if l.get("user_id") == rec.get("user_id")]
    except DSSError:
        logs = []
    return {
        "prefix": rec.get("prefix"),
        "credits": int(user.get("credits", 0)),
        "total_operations": len(logs),
        "successful": sum(1 for l in logs if l.get("status") == "success"),
        "created_at": user.get("created_at"),
    }


# ─────────── أدوات مساعدة للصور ───────────
async def _check_credit(rec: dict):
    user = await get_user_by_id(rec["user_id"])
    if not user:
        raise HTTPException(404, "المستخدم غير موجود")
    if int(user.get("credits", 0)) < 1:
        raise HTTPException(402, "رصيدك من النقاط انتهى")


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) == 0:
        raise HTTPException(400, "الملف فارغ")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, f"حجم الملف يتجاوز {MAX_FILE_MB} ميجابايت")
    return data


# ─────────── نقاط معالجة الصور ───────────
@app.post("/api/remove-bg")
async def api_remove_bg(
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None),
):
    rec = await authenticate(x_api_key)
    await _check_credit(rec)
    data = await _read_upload(file)

    t0 = time.time()
    try:
        result = await asyncio.to_thread(process_remove_bg, data)
    except Exception as e:
        await log_usage(rec["user_id"], "remove-bg", "error", int((time.time() - t0) * 1000))
        raise HTTPException(500, str(e))

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], "remove-bg", "success", int((time.time() - t0) * 1000))
    return Response(
        content=result,
        media_type="image/png",
        headers={"Content-Disposition": 'attachment; filename="no_bg.png"'},
    )


@app.post("/api/enhance")
async def api_enhance(
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None),
):
    rec = await authenticate(x_api_key)
    await _check_credit(rec)
    data = await _read_upload(file)

    t0 = time.time()
    try:
        result, ext = await asyncio.to_thread(process_enhance, data)
    except Exception as e:
        await log_usage(rec["user_id"], "enhance", "error", int((time.time() - t0) * 1000))
        raise HTTPException(500, str(e))

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], "enhance", "success", int((time.time() - t0) * 1000))
    return Response(
        content=result,
        media_type=f"image/{ext}",
        headers={"Content-Disposition": f'attachment; filename="enhanced.{ext}"'},
    )


@app.post("/api/compress")
async def api_compress(
    file: UploadFile = File(...),
    quality: int = 75,
    x_api_key: Optional[str] = Header(None),
):
    rec = await authenticate(x_api_key)
    await _check_credit(rec)
    data = await _read_upload(file)
    quality = max(10, min(95, quality))

    t0 = time.time()
    try:
        result = await asyncio.to_thread(process_compress, data, quality)
    except Exception as e:
        await log_usage(rec["user_id"], "compress", "error", int((time.time() - t0) * 1000))
        raise HTTPException(500, str(e))

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], "compress", "success", int((time.time() - t0) * 1000))
    return Response(
        content=result,
        media_type="image/jpeg",
        headers={"Content-Disposition": 'attachment; filename="compressed.jpg"'},
    )


@app.post("/api/convert")
async def api_convert(
    file: UploadFile = File(...),
    target: str = "png",
    x_api_key: Optional[str] = Header(None),
):
    rec = await authenticate(x_api_key)
    await _check_credit(rec)
    data = await _read_upload(file)

    t0 = time.time()
    try:
        result, mime, ext = await asyncio.to_thread(process_convert, data, target)
    except Exception as e:
        await log_usage(rec["user_id"], "convert", "error", int((time.time() - t0) * 1000))
        raise HTTPException(500, str(e))

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], "convert", "success", int((time.time() - t0) * 1000))
    return Response(
        content=result,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="converted.{ext}"'},
    )


@app.post("/api/grayscale")
async def api_grayscale(
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None),
):
    rec = await authenticate(x_api_key)
    await _check_credit(rec)
    data = await _read_upload(file)

    t0 = time.time()
    try:
        result = await asyncio.to_thread(process_grayscale, data)
    except Exception as e:
        await log_usage(rec["user_id"], "grayscale", "error", int((time.time() - t0) * 1000))
        raise HTTPException(500, str(e))

    await deduct_credit(rec["user_id"])
    await log_usage(rec["user_id"], "grayscale", "success", int((time.time() - t0) * 1000))
    return Response(
        content=result,
        media_type="image/png",
        headers={"Content-Disposition": 'attachment; filename="grayscale.png"'},
    )


@app.get("/api/health")
async def health():
    result = {"status": "ok", "rembg": HAS_REMBG, "version": "3.0.0"}
    try:
        users = await dss.list("users")
        result["dss"] = "connected"
        result["dss_users"] = len(users)
    except Exception as e:
        result["dss"] = f"error: {e}"
        result["status"] = "degraded"
    return result


# ═══════════════════════════════════════════════════════════════════
#  [8] الصفحة (HTML + CSS + JS مضمّنة)
# ═══════════════════════════════════════════════════════════════════
HTML_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#00e0ff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="mobile-web-app-capable" content="yes">
<title>Lux Image Studio</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#05070f; --bg2:#0a0e1a;
  --surface:rgba(255,255,255,.035);
  --surface2:rgba(255,255,255,.06);
  --border:rgba(255,255,255,.08);
  --text:#e8ecf4; --muted:#8a94a8;
  --cyan:#00e0ff; --purple:#7c5cff; --pink:#ff2d95;
  --success:#00ffa3; --danger:#ff4d6d; --warn:#ffb020;
  --grad:linear-gradient(135deg,#00e0ff 0%,#7c5cff 50%,#ff2d95 100%);
  --shadow:0 20px 60px rgba(0,0,0,.5);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:'Tajawal',system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  min-height:100vh;line-height:1.6;overflow-x:hidden;
  -webkit-tap-highlight-color:transparent;
}
body::before{
  content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(circle at 15% 20%,rgba(0,224,255,.12),transparent 35%),
    radial-gradient(circle at 85% 15%,rgba(124,92,255,.14),transparent 35%),
    radial-gradient(circle at 50% 90%,rgba(255,45,149,.09),transparent 40%);
}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-track{background:var(--bg2)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
a{color:var(--cyan);text-decoration:none}
code{font-family:'JetBrains Mono',monospace;font-size:.88em}

header{
  position:sticky;top:0;z-index:100;
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  background:rgba(5,7,15,.75);
  border-bottom:1px solid var(--border);
  padding:14px 20px;
  display:flex;align-items:center;justify-content:space-between;
}
.logo{font-weight:900;font-size:1.3rem;letter-spacing:-.5px}
.logo .dot{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.nav{display:flex;gap:14px;align-items:center}
.nav a{color:var(--muted);font-weight:500;font-size:.85rem;transition:color .2s}
.nav a:hover{color:var(--text)}
.badge{
  padding:4px 10px;border-radius:20px;font-size:.72rem;font-weight:700;
  background:rgba(0,255,163,.15);color:var(--success);
  border:1px solid rgba(0,255,163,.3);
}
@media(max-width:500px){ .nav a:not(.keep){display:none} }

main{max-width:1000px;margin:0 auto;padding:24px 16px 60px}

.hero{text-align:center;padding:30px 0 40px}
.hero h1{
  font-size:clamp(1.8rem,5vw,3.2rem);font-weight:900;line-height:1.15;
  letter-spacing:-1.2px;margin-bottom:16px;
}
.hero h1 .grad{
  background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;
}
.hero p{font-size:1.05rem;color:var(--muted);max-width:560px;margin:0 auto 28px}
@media(max-width:500px){.hero p{font-size:.95rem}}

.btn{
  display:inline-flex;align-items:center;gap:8px;justify-content:center;
  padding:13px 24px;border-radius:12px;border:none;cursor:pointer;
  font-family:inherit;font-size:.95rem;font-weight:700;
  transition:all .2s;letter-spacing:.2px;
}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{
  background:var(--grad);color:#05070f;
  box-shadow:0 8px 30px rgba(124,92,255,.4);
}
.btn-primary:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 12px 40px rgba(124,92,255,.5)}
.btn-secondary{
  background:var(--surface2);color:var(--text);border:1px solid var(--border);
}
.btn-secondary:hover:not(:disabled){background:rgba(255,255,255,.1)}
.btn-ghost{
  background:transparent;color:var(--muted);border:1px solid var(--border);
  padding:8px 14px;font-size:.85rem;
}
.btn-ghost:hover{color:var(--text);background:var(--surface)}

.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:18px;padding:24px;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
}
@media(max-width:500px){.card{padding:18px}}

.key-banner{
  background:linear-gradient(135deg,rgba(0,224,255,.08),rgba(124,92,255,.08));
  border:1px solid rgba(0,224,255,.25);border-radius:18px;
  padding:22px;margin-bottom:24px;text-align:center;
}
.key-banner h3{font-size:1.15rem;margin-bottom:6px}
.key-banner p{color:var(--muted);margin-bottom:16px;font-size:.9rem}
.key-display{
  display:flex;gap:8px;background:#05070f;border:1px solid var(--border);
  border-radius:10px;padding:12px 14px;align-items:center;
  font-family:'JetBrains Mono',monospace;font-size:.82rem;
  margin-bottom:10px;word-break:break-all;
}
.key-display span{flex:1;color:var(--cyan);text-align:left;direction:ltr;overflow:hidden}
.key-display button{
  background:var(--surface2);border:1px solid var(--border);
  color:var(--text);padding:7px 12px;border-radius:8px;
  cursor:pointer;font-family:inherit;font-weight:700;font-size:.78rem;
  transition:all .2s;white-space:nowrap;
}
.key-display button:hover{background:rgba(255,255,255,.12)}
.warn-text{color:var(--warn);font-size:.8rem}

.section{margin-bottom:28px}
.section h2{font-size:1.4rem;margin-bottom:16px;font-weight:700}

.upload-zone{
  border:2px dashed var(--border);border-radius:18px;
  padding:50px 20px;text-align:center;cursor:pointer;
  transition:all .25s;background:var(--surface);
}
.upload-zone:hover,.upload-zone.dragover{
  border-color:var(--cyan);background:rgba(0,224,255,.05);
}
.upload-zone svg{width:48px;height:48px;stroke:var(--cyan);margin-bottom:12px}
.upload-zone h3{font-size:1.05rem;margin-bottom:4px}
.upload-zone p{color:var(--muted);font-size:.85rem}
.upload-zone input{display:none}
@media(max-width:500px){.upload-zone{padding:36px 16px}}

.preview-row{
  display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px;
}
@media(max-width:600px){.preview-row{grid-template-columns:1fr}}
.img-box{
  background:var(--surface);border:1px solid var(--border);
  border-radius:14px;padding:12px;text-align:center;
}
.img-box .label{
  font-size:.72rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:1px;font-weight:700;margin-bottom:10px;
}
.img-box img{
  max-width:100%;max-height:360px;border-radius:8px;
  display:block;margin:0 auto;
}
.checker{
  background-image:
    linear-gradient(45deg,#1a1f2e 25%,transparent 25%),
    linear-gradient(-45deg,#1a1f2e 25%,transparent 25%),
    linear-gradient(45deg,transparent 75%,#1a1f2e 75%),
    linear-gradient(-45deg,transparent 75%,#1a1f2e 75%);
  background-size:16px 16px;
  background-position:0 0,0 8px,8px -8px,-8px 0;
  background-color:#0d1220;
  border-radius:8px;padding:6px;
}

.actions{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:10px;margin-top:16px;
}
.actions .btn{width:100%}

.loading{display:flex;flex-direction:column;align-items:center;padding:40px;gap:14px}
.spinner{
  width:44px;height:44px;border-radius:50%;
  border:4px solid var(--border);border-top-color:var(--cyan);
  animation:spin 1s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.loading p{color:var(--muted);font-size:.9rem}

.stats-grid{
  display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
}
@media(max-width:500px){.stats-grid{grid-template-columns:1fr}}
.stat{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:16px;text-align:center;
}
.stat .num{
  font-size:1.5rem;font-weight:900;
  background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;
  font-family:'JetBrains Mono',monospace;
}
.stat .lbl{font-size:.78rem;color:var(--muted);margin-top:2px}

.code-block{
  background:#05070f;border:1px solid var(--border);border-radius:10px;
  padding:16px;overflow-x:auto;
  font-family:'JetBrains Mono',monospace;font-size:.76rem;
  line-height:1.7;color:#c9d1e0;
  direction:ltr;text-align:left;
  white-space:pre;
}
.code-block .cmt{color:#6272a4}
.code-block .str{color:#50fa7b}
.code-block .kw{color:#ff79c6}

.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(150%);
  background:#0d1220;border:1px solid var(--border);
  padding:12px 22px;border-radius:12px;
  box-shadow:var(--shadow);z-index:999;font-weight:600;font-size:.9rem;
  transition:transform .3s;max-width:calc(100% - 32px);text-align:center;
}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.error{border-color:rgba(255,77,109,.4);color:var(--danger)}
.toast.success{border-color:rgba(0,255,163,.4);color:var(--success)}

.install-btn{
  display:none;position:fixed;bottom:16px;left:16px;z-index:200;
  padding:12px 20px;border-radius:30px;border:none;cursor:pointer;
  background:var(--grad);color:#05070f;font-family:inherit;font-weight:800;font-size:.85rem;
  box-shadow:0 10px 30px rgba(124,92,255,.5);
}
.install-btn.show{display:flex;align-items:center;gap:6px}

.hidden{display:none !important}

footer{
  text-align:center;padding:28px 16px;color:var(--muted);font-size:.8rem;
  border-top:1px solid var(--border);margin-top:40px;
}
</style>
</head>
<body>

<header>
  <div class="logo">Lux<span class="dot">.</span>Studio</div>
  <nav class="nav">
    <a href="#studio" class="keep">الاستوديو</a>
    <a href="#api-panel" class="keep">API</a>
    <a href="#docs">التوثيق</a>
    <span class="badge" id="status-badge">● جاهز</span>
  </nav>
</header>

<button class="install-btn" id="install-btn" onclick="doInstall()">📱 تثبيت التطبيق</button>

<main>

  <section class="hero">
    <h1>استوديو احترافي<br><span class="grad">بين يديك</span></h1>
    <p>إزالة خلفيات، تحسين جودة، ضغط، تحويل صيغ — كل ذلك بضغطة زر</p>
    <div id="hero-actions">
      <button class="btn btn-primary" id="get-key-btn" onclick="getFreeKey()">
        🚀 احصل على مفتاح مجاني — 20 عملية
      </button>
      <button class="btn btn-primary hidden" id="start-btn" onclick="scrollToStudio()">
        ✨ ابدأ الآن
      </button>
    </div>
  </section>

  <div class="key-banner hidden" id="new-key-banner">
    <h3>🎉 تم إنشاء مفتاحك</h3>
    <p>احفظه الآن — لن يظهر مرة أخرى</p>
    <div class="key-display">
      <span id="new-key-value">-</span>
      <button onclick="copyNewKey()">📋 نسخ</button>
    </div>
    <p class="warn-text">⚠️ فقدانه يعني عدم القدرة على الاستخدام</p>
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
        <div class="stat">
          <div class="num" id="stat-credits">-</div>
          <div class="lbl">نقطة متبقية</div>
        </div>
        <div class="stat">
          <div class="num" id="stat-ops">-</div>
          <div class="lbl">إجمالي العمليات</div>
        </div>
        <div class="stat">
          <div class="num" id="stat-success">-</div>
          <div class="lbl">ناجحة</div>
        </div>
      </div>
    </div>
  </section>

  <section class="section hidden" id="studio">
    <h2>🎨 الاستوديو</h2>

    <div class="upload-zone" id="drop-zone">
      <input type="file" id="file-input" accept="image/*">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <h3>اسحب صورة أو اضغط للاختيار</h3>
      <p>PNG · JPG · WEBP — حتى 15 ميجابايت</p>
    </div>

    <div class="hidden" id="workspace">
      <div class="preview-row">
        <div class="img-box">
          <div class="label">الأصلية</div>
          <img id="original-img" alt="">
        </div>
        <div class="img-box hidden" id="result-box">
          <div class="label" id="result-label">النتيجة</div>
          <div class="checker" id="result-checker">
            <img id="result-img" alt="">
          </div>
        </div>
      </div>

      <div class="actions" id="action-buttons">
        <button class="btn btn-primary" onclick="runAction('remove-bg')">🖼️ إزالة الخلفية</button>
        <button class="btn btn-secondary" onclick="runAction('enhance')">✨ تحسين</button>
        <button class="btn btn-secondary" onclick="runAction('compress')">📉 ضغط</button>
        <button class="btn btn-secondary" onclick="runAction('grayscale')">⚫ رمادي</button>
        <button class="btn btn-secondary" onclick="runAction('convert', {target:'png'})">🔄 PNG</button>
        <button class="btn btn-secondary" onclick="runAction('convert', {target:'webp'})">🔄 WEBP</button>
        <button class="btn btn-ghost" onclick="resetStudio()">🆕 صورة جديدة</button>
      </div>

      <div class="hidden" id="loading">
        <div class="loading">
          <div class="spinner"></div>
          <p>⏳ جاري المعالجة...</p>
        </div>
      </div>

      <div class="hidden" id="download-row" style="text-align:center;margin-top:16px">
        <a id="download-link" class="btn btn-primary" download>⬇️ تحميل النتيجة</a>
      </div>
    </div>
  </section>

  <section class="section" id="docs">
    <h2>📖 التوثيق</h2>
    <div class="card">
      <p style="color:var(--muted);margin-bottom:14px;font-size:.9rem">
        استخدم مفتاحك من أي تطبيق خارجي عبر الهيدر
        <code style="background:#05070f;padding:2px 8px;border-radius:6px">X-API-Key</code>:
      </p>
      <div class="code-block"><span class="cmt"># إزالة الخلفية</span>
curl -X POST "http://YOUR_HOST/api/remove-bg" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output result.png

<span class="cmt"># تحسين الجودة</span>
curl -X POST "http://YOUR_HOST/api/enhance" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output enhanced.jpg

<span class="cmt"># ضغط الصورة</span>
curl -X POST "http://YOUR_HOST/api/compress?quality=70" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output small.jpg

<span class="cmt"># تحويل الصيغة</span>
curl -X POST "http://YOUR_HOST/api/convert?target=png" \\
  -H "X-API-Key: YOUR_KEY" \\
  -F "file=@image.jpg" --output image.png

<span class="cmt"># معلومات حسابك</span>
curl "http://YOUR_HOST/api/me" -H "X-API-Key: YOUR_KEY"
</div>
    </div>
  </section>

</main>

<footer>Lux Image Studio © 2025 — صُنع بعناية ✨</footer>

<div class="toast" id="toast"></div>

<script>
const state = {
  key: localStorage.getItem('lux_api_key') || null,
  lastFile: null,
  keyVisible: false,
  deferredPrompt: null,
};

function toast(msg, type='success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.className = 'toast ' + type, 3500);
}

window.addEventListener('DOMContentLoaded', () => {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(()=>{});
  }
  if (state.key) {
    showStudio();
    refreshUser();
  } else {
    showWelcome();
  }
  checkHealth();
});

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  state.deferredPrompt = e;
  document.getElementById('install-btn').classList.add('show');
});

function doInstall() {
  if (!state.deferredPrompt) return;
  state.deferredPrompt.prompt();
  state.deferredPrompt.userChoice.then(() => {
    state.deferredPrompt = null;
    document.getElementById('install-btn').classList.remove('show');
  });
}

async function checkHealth() {
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    const badge = document.getElementById('status-badge');
    if (d.status === 'ok') {
      badge.textContent = '● متصل';
      badge.style.color = 'var(--success)';
    } else {
      badge.textContent = '● قاعدة البيانات';
      badge.style.color = 'var(--warn)';
      badge.title = 'DSS: ' + (d.dss || 'خطأ');
    }
  } catch (e) {}
}

function showWelcome() {
  document.getElementById('studio').classList.add('hidden');
  document.getElementById('api-panel').classList.add('hidden');
  document.getElementById('get-key-btn').classList.remove('hidden');
  document.getElementById('start-btn').classList.add('hidden');
}

function showStudio() {
  document.getElementById('studio').classList.remove('hidden');
  document.getElementById('api-panel').classList.remove('hidden');
  document.getElementById('get-key-btn').classList.add('hidden');
  document.getElementById('start-btn').classList.remove('hidden');
}

function scrollToStudio() {
  document.getElementById('studio').scrollIntoView({behavior:'smooth'});
}

async function getFreeKey() {
  const btn = document.getElementById('get-key-btn');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = '⏳ جاري الإنشاء...';
  try {
    const r = await fetch('/api/keys/new', {method:'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'فشل');
    state.key = d.api_key;
    localStorage.setItem('lux_api_key', d.api_key);
    document.getElementById('new-key-value').textContent = d.api_key;
    document.getElementById('new-key-banner').classList.remove('hidden');
    showStudio();
    await refreshUser();
    document.getElementById('new-key-banner').scrollIntoView({behavior:'smooth', block:'center'});
    toast('✅ تم إنشاء المفتاح', 'success');
  } catch (e) {
    toast('❌ ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = original;
  }
}

function copyNewKey() {
  const v = document.getElementById('new-key-value').textContent;
  navigator.clipboard.writeText(v).then(()=>toast('📋 تم النسخ'));
}

function maskKey(k) {
  if (!k) return '-';
  return k.slice(0,10) + '••••••' + k.slice(-4);
}

function updateKeyDisplay() {
  const el = document.getElementById('api-key-masked');
  el.textContent = state.keyVisible ? state.key : maskKey(state.key);
  document.getElementById('toggle-key-btn').textContent = state.keyVisible ? '🙈' : '👁';
}

function toggleKeyVisibility() {
  state.keyVisible = !state.keyVisible;
  updateKeyDisplay();
}

function copyApiKey() {
  if (!state.key) return;
  navigator.clipboard.writeText(state.key).then(()=>toast('📋 تم النسخ'));
}

async function refreshUser() {
  if (!state.key) return;
  try {
    const r = await fetch('/api/me', {headers:{'X-API-Key':state.key}});
    if (r.status === 401) {
      localStorage.removeItem('lux_api_key');
      state.key = null;
      showWelcome();
      toast('⚠️ مفتاحك غير صالح', 'error');
      return;
    }
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('stat-credits').textContent = d.credits;
    document.getElementById('stat-ops').textContent = d.total_operations;
    document.getElementById('stat-success').textContent = d.successful;
    updateKeyDisplay();
  } catch (e) {}
}

const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => {e.preventDefault(); dropZone.classList.add('dragover');});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e => {
  if (e.target.files.length) handleFile(e.target.files[0]);
});

function handleFile(file) {
  if (!file.type.startsWith('image/')) {
    toast('❌ الملف ليس صورة', 'error');
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    toast('❌ الحجم يتجاوز 15 ميجابايت', 'error');
    return;
  }
  state.lastFile = file;
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('original-img').src = e.target.result;
    document.getElementById('workspace').classList.remove('hidden');
    document.getElementById('result-box').classList.add('hidden');
    document.getElementById('download-row').classList.add('hidden');
    document.getElementById('action-buttons').classList.remove('hidden');
    document.getElementById('workspace').scrollIntoView({behavior:'smooth', block:'start'});
  };
  reader.readAsDataURL(file);
}

async function runAction(action, params = {}) {
  if (!state.lastFile) return toast('❌ اختر صورة أولاً', 'error');
  if (!state.key) return toast('❌ مفتاح مفقود', 'error');

  const btnContainer = document.getElementById('action-buttons');
  const loading = document.getElementById('loading');
  const resultBox = document.getElementById('result-box');
  const dlRow = document.getElementById('download-row');
  btnContainer.classList.add('hidden');
  loading.classList.remove('hidden');
  resultBox.classList.add('hidden');
  dlRow.classList.add('hidden');

  const t0 = performance.now();
  try {
    const form = new FormData();
    form.append('file', state.lastFile);
    const qs = new URLSearchParams(params).toString();
    const url = '/api/' + action + (qs ? '?' + qs : '');

    const r = await fetch(url, {
      method:'POST',
      headers: {'X-API-Key': state.key},
      body: form
    });

    if (!r.ok) {
      const err = await r.json().catch(()=>({detail:`خطأ ${r.status}`}));
      throw new Error(err.detail || 'فشل');
    }

    const blob = await r.blob();
    const objUrl = URL.createObjectURL(blob);
    document.getElementById('result-img').src = objUrl;
    document.getElementById('result-label').textContent =
      action === 'remove-bg' ? 'بدون خلفية' :
      action === 'enhance' ? 'محسّنة' :
      action === 'compress' ? 'مضغوطة' :
      action === 'grayscale' ? 'رمادية' : 'محوّلة';
    document.getElementById('result-checker').style.background =
      action === 'remove-bg' ? '' : 'transparent';
    resultBox.classList.remove('hidden');

    const dl = document.getElementById('download-link');
    const ext = blob.type === 'image/png' ? 'png' : blob.type === 'image/webp' ? 'webp' : 'jpg';
    dl.href = objUrl;
    dl.download = `lux_${action}.${ext}`;
    dlRow.classList.remove('hidden');

    const dt = Math.round(performance.now() - t0);
    toast(`✅ تمت في ${dt}ms`, 'success');
    await refreshUser();
  } catch (e) {
    toast('❌ ' + e.message, 'error');
    btnContainer.classList.remove('hidden');
  } finally {
    loading.classList.add('hidden');
  }
}

function resetStudio() {
  state.lastFile = null;
  fileInput.value = '';
  document.getElementById('workspace').classList.add('hidden');
  dropZone.scrollIntoView({behavior:'smooth', block:'center'});
}
</script>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
#  [9] الأيقونة و Service Worker (لتثبيت PWA)
# ═══════════════════════════════════════════════════════════════════
SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#00e0ff"/>
<stop offset="0.5" stop-color="#7c5cff"/>
<stop offset="1" stop-color="#ff2d95"/>
</linearGradient>
</defs>
<rect width="512" height="512" rx="112" fill="#05070f"/>
<circle cx="256" cy="256" r="140" fill="none" stroke="url(#g)" stroke-width="20"/>
<path d="M200 320 L256 200 L312 320 Z" fill="url(#g)"/>
<circle cx="256" cy="180" r="22" fill="url(#g)"/>
</svg>"""


SW_JS = """// Lux Studio Service Worker — v1
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  if (e.request.url.includes('/api/')) return;
  if (e.request.url.endsWith('/ping')) return;
  e.respondWith(
    caches.open('lux-v1').then(cache =>
      fetch(e.request).then(res => {
        if (res.ok && res.type === 'basic') cache.put(e.request, res.clone());
        return res;
      }).catch(() => cache.match(e.request))
    )
  );
});
"""


# ═══════════════════════════════════════════════════════════════════
#  [10] التشغيل
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  🌐 Lux Image Studio v3.0                                ║")
    print(f"║  🌍 http://0.0.0.0:{PORT}                                  ║")
    print(f"║  📦 DSS: {DSS_BASE_URL[:42]:<42}  ║")
    print(f"║  🎨 rembg: {'✅' if HAS_REMBG else '❌'}                                       ║")
    print("╚══════════════════════════════════════════════════════════╝")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
