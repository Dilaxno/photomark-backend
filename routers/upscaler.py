from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from PIL import Image
import io
import os
import gc
import torch
import httpx
from realesrgan import RealESRGAN
from core.config import logger  # type: ignore

router = APIRouter(prefix="/api/upscaler", tags=["upscaler"])

_realesrgan = None

def _weights_path() -> str:
    base = os.path.expanduser(os.getenv("REAL_ESRGAN_HOME", "~/.realesrgan/weights"))
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, "RealESRGAN_x4plus.pth")

async def _ensure_weights(path: str):
    if os.path.isfile(path):
        return
    url = os.getenv("REAL_ESRGAN_WEIGHTS_URL", "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5/RealESRGAN_x4plus.pth")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
    except Exception as e:
        logger.error(f"Failed to download RealESRGAN weights: {e}")
        raise HTTPException(status_code=503, detail="Upscaler weights unavailable")

async def _get_upscaler():
    global _realesrgan
    if _realesrgan is None:
        path = _weights_path()
        await _ensure_weights(path)
        try:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            model = RealESRGAN(device, scale=4)
            model.load_weights(path)
            _realesrgan = model
            logger.info("RealESRGAN upscaler initialized")
        except Exception as e:
            logger.error(f"Failed to init RealESRGAN: {e}")
            _realesrgan = None
    return _realesrgan

def _resize_if_needed(img: Image.Image) -> Image.Image:
    try:
        max_long = int(os.getenv("UPSCALER_MAX_LONG_EDGE", "1024"))
    except Exception:
        max_long = 1024
    w, h = img.width, img.height
    long_edge = w if w >= h else h
    if long_edge <= max_long:
        return img
    scale = max_long / float(long_edge)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)

@router.post("/preview")
async def preview(image: UploadFile = File(...)):
    try:
        data = await image.read()
        inp = Image.open(io.BytesIO(data)).convert("RGB")
        inp = _resize_if_needed(inp)
        model = await _get_upscaler()
        if model is None:
            raise HTTPException(status_code=503, detail="Upscaler model not available")
        out = model.predict(inp)
        if not isinstance(out, Image.Image):
            raise HTTPException(status_code=500, detail="Upscaler did not return an image")
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)
        import base64
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        res = {"success": True, "preview": b64, "width": out.width, "height": out.height}
        try:
            del inp
            del out
            del buf
            gc.collect()
        except Exception:
            pass
        return res
    except HTTPException:
        raise
    except MemoryError:
        raise HTTPException(status_code=413, detail="Image too large to upscale on this server")
    except Exception as e:
        logger.error(f"Upscaler preview failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/download")
async def download(image: UploadFile = File(...), output_format: str = Form("png")):
    try:
        data = await image.read()
        inp = Image.open(io.BytesIO(data)).convert("RGB")
        inp = _resize_if_needed(inp)
        model = await _get_upscaler()
        if model is None:
            raise HTTPException(status_code=503, detail="Upscaler model not available")
        out = model.predict(inp)
        if not isinstance(out, Image.Image):
            raise HTTPException(status_code=500, detail="Upscaler did not return an image")
        fmt = "PNG" if output_format.lower() == "png" else "JPEG"
        buf = io.BytesIO()
        if fmt == "JPEG":
            out = out.convert("RGB")
        out.save(buf, format=fmt, quality=95)
        buf.seek(0)
        headers = {"Content-Disposition": f"attachment; filename=upscaled.{output_format.lower()}"}
        resp = StreamingResponse(buf, media_type=f"image/{output_format.lower()}", headers=headers)
        try:
            del inp
            del out
            gc.collect()
        except Exception:
            pass
        return resp
    except HTTPException:
        raise
    except MemoryError:
        raise HTTPException(status_code=413, detail="Image too large to upscale on this server")
    except Exception as e:
        logger.error(f"Upscaler download failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
