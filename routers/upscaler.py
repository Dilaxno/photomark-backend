from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from PIL import Image
import io
import os
import gc
import numpy as np
from core.config import logger  # type: ignore
from core.auth import get_uid_from_request
from utils.rate_limit import check_processing_rate_limit, validate_file_size

# Try to import cv2 for high-quality upscaling
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("OpenCV not available, falling back to Pillow for upscaling")

router = APIRouter(prefix="/api/upscaler", tags=["upscaler"])


def _upscale_image(img: Image.Image, scale: int = 4) -> Image.Image:
    """
    Upscale image using lightweight methods.
    Uses OpenCV INTER_LANCZOS4 if available, otherwise Pillow LANCZOS.
    """
    new_width = img.width * scale
    new_height = img.height * scale
    
    if CV2_AVAILABLE:
        # Convert PIL to numpy array
        img_array = np.array(img)
        # Convert RGB to BGR for OpenCV
        if len(img_array.shape) == 3 and img_array.shape[2] == 3:
            img_array = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
        
        # Use INTER_LANCZOS4 for high-quality upscaling
        upscaled = cv2.resize(img_array, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)
        
        # Apply subtle sharpening to enhance details
        kernel = np.array([[-0.1, -0.1, -0.1],
                          [-0.1,  1.8, -0.1],
                          [-0.1, -0.1, -0.1]])
        upscaled = cv2.filter2D(upscaled, -1, kernel)
        upscaled = np.clip(upscaled, 0, 255).astype(np.uint8)
        
        # Convert BGR back to RGB
        if len(upscaled.shape) == 3 and upscaled.shape[2] == 3:
            upscaled = cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB)
        
        return Image.fromarray(upscaled)
    else:
        # Fallback to Pillow LANCZOS
        return img.resize((new_width, new_height), Image.LANCZOS)


def _resize_if_needed(img: Image.Image) -> Image.Image:
    """Resize input image if it exceeds max dimensions to prevent memory issues."""
    try:
        max_long = int(os.getenv("UPSCALER_MAX_LONG_EDGE", "1024"))
    except Exception:
        max_long = 1024
    w, h = img.width, img.height
    long_edge = max(w, h)
    if long_edge <= max_long:
        return img
    scale = max_long / float(long_edge)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)


@router.post("/preview")
async def preview(request: Request, image: UploadFile = File(...), scale: int = Form(4)):
    """Generate upscaled preview of the image."""
    # Rate limiting
    uid = get_uid_from_request(request)
    rate_key = uid if uid else (request.client.host if request.client else "unknown")
    allowed, rate_err = check_processing_rate_limit(rate_key)
    if not allowed:
        raise HTTPException(status_code=429, detail=rate_err)
    
    # Validate scale factor
    if scale not in [2, 4]:
        scale = 4
    
    try:
        data = await image.read()
        
        # Validate file size
        valid, err = validate_file_size(len(data), image.filename or '')
        if not valid:
            raise HTTPException(status_code=400, detail=err)
        
        inp = Image.open(io.BytesIO(data)).convert("RGB")
        inp = _resize_if_needed(inp)
        
        # Upscale the image
        out = _upscale_image(inp, scale=scale)
        
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)
        
        import base64
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        res = {"success": True, "preview": b64, "width": out.width, "height": out.height}
        
        # Cleanup
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
async def download(
    request: Request,
    image: UploadFile = File(...),
    output_format: str = Form("png"),
    scale: int = Form(4)
):
    """Download upscaled image."""
    # Rate limiting
    uid = get_uid_from_request(request)
    rate_key = uid if uid else (request.client.host if request.client else "unknown")
    allowed, rate_err = check_processing_rate_limit(rate_key)
    if not allowed:
        raise HTTPException(status_code=429, detail=rate_err)
    
    # Validate scale factor
    if scale not in [2, 4]:
        scale = 4
    
    try:
        data = await image.read()
        
        # Validate file size
        valid, err = validate_file_size(len(data), image.filename or '')
        if not valid:
            raise HTTPException(status_code=400, detail=err)
        
        inp = Image.open(io.BytesIO(data)).convert("RGB")
        inp = _resize_if_needed(inp)
        
        # Upscale the image
        out = _upscale_image(inp, scale=scale)
        
        fmt = "PNG" if output_format.lower() == "png" else "JPEG"
        buf = io.BytesIO()
        if fmt == "JPEG":
            out = out.convert("RGB")
        out.save(buf, format=fmt, quality=95)
        buf.seek(0)
        
        headers = {"Content-Disposition": f"attachment; filename=upscaled_{scale}x.{output_format.lower()}"}
        resp = StreamingResponse(buf, media_type=f"image/{output_format.lower()}", headers=headers)
        
        # Cleanup
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
