from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from PIL import Image
import io
from core.config import logger  # type: ignore

try:
    from transformers import pipeline as hf_pipeline  # type: ignore
    HF_UPSCALER_AVAILABLE = True
except Exception:
    HF_UPSCALER_AVAILABLE = False
    logger.warning("transformers pipeline not available for Swin2SR")

router = APIRouter(prefix="/api/upscaler", tags=["upscaler"])

_hf_upscaler = None

def _get_upscaler():
    global _hf_upscaler
    if _hf_upscaler is None:
        try:
            _hf_upscaler = hf_pipeline("image-to-image", model="caidas/swin2SR-compressed-sr-x4-48")
            logger.info("Swin2SR upscaler pipeline loaded")
        except Exception as e:
            logger.error(f"Failed to load Swin2SR pipeline: {e}")
            _hf_upscaler = None
    return _hf_upscaler

@router.post("/preview")
async def preview(image: UploadFile = File(...)):
    try:
        data = await image.read()
        inp = Image.open(io.BytesIO(data)).convert("RGB")
        pipe = _get_upscaler()
        if pipe is None:
            raise HTTPException(status_code=503, detail="Upscaler model not available")
        out = pipe(inp)
        if not isinstance(out, Image.Image):
            raise HTTPException(status_code=500, detail="Upscaler did not return an image")
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)
        import base64
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return {"success": True, "preview": b64, "width": out.width, "height": out.height}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upscaler preview failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/download")
async def download(image: UploadFile = File(...), output_format: str = Form("png")):
    try:
        data = await image.read()
        inp = Image.open(io.BytesIO(data)).convert("RGB")
        pipe = _get_upscaler()
        if pipe is None:
            raise HTTPException(status_code=503, detail="Upscaler model not available")
        out = pipe(inp)
        if not isinstance(out, Image.Image):
            raise HTTPException(status_code=500, detail="Upscaler did not return an image")
        fmt = "PNG" if output_format.lower() == "png" else "JPEG"
        buf = io.BytesIO()
        if fmt == "JPEG":
            out = out.convert("RGB")
        out.save(buf, format=fmt, quality=95)
        buf.seek(0)
        return StreamingResponse(buf, media_type=f"image/{output_format.lower()}", headers={"Content-Disposition": f"attachment; filename=upscaled.{output_format.lower()}"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upscaler download failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

