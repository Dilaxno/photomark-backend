from __future__ import annotations

import os
import logging
from io import BytesIO
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image
import cv2

# Try to import Aydin for advanced denoising
try:
    from aydin import Denoise
    AYDIN_AVAILABLE = True
    logger = logging.getLogger(__name__)
    logger.info("Aydin library loaded successfully")
except ImportError:
    AYDIN_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("Aydin library not available, falling back to OpenCV")

router = APIRouter()


def _denoise_aydin(img: np.ndarray, strength: float) -> np.ndarray:
    """Denoise using Aydin library with automatic method selection."""
    try:
        # Convert RGB to grayscale for Aydin (it works better with single channel)
        if img.ndim == 3 and img.shape[2] == 3:
            # Process each channel separately for better results
            denoised_channels = []
            for channel in range(3):
                channel_img = img[:, :, channel].astype(np.float32)
                
                # Create Aydin denoiser
                denoiser = Denoise()
                
                # Aydin automatically selects the best method
                denoised_channel = denoiser.denoise(channel_img)
                
                # Apply strength blending
                if 0.0 <= strength <= 1.0:
                    denoised_channel = (1 - strength) * channel_img + strength * denoised_channel
                
                denoised_channels.append(denoised_channel)
            
            # Combine channels back
            denoised = np.stack(denoised_channels, axis=2)
        else:
            # Single channel image
            img_float = img.astype(np.float32)
            denoiser = Denoise()
            denoised = denoiser.denoise(img_float)
            
            # Apply strength blending
            if 0.0 <= strength <= 1.0:
                denoised = (1 - strength) * img_float + strength * denoised
        
        # Convert back to uint8
        denoised = np.clip(denoised, 0, 255).astype(np.uint8)
        return denoised
        
    except Exception as e:
        logger.error(f"Aydin denoising failed: {e}")
        # Fallback to OpenCV if Aydin fails
        return _denoise_cv2(img, strength)


def _denoise_cv2(img: np.ndarray, strength: float) -> np.ndarray:
    # Colored denoising as fallback; strength maps to h parameters
    h_color = int(5 + strength * 15)
    h_luma = int(5 + strength * 15)
    # cv2 expects BGR
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    den = cv2.fastNlMeansDenoisingColored(bgr, None, h_luma, h_color, 7, 21)
    rgb = cv2.cvtColor(den, cv2.COLOR_BGR2RGB)
    return rgb


def _read_image_keep_alpha(data: bytes) -> tuple[np.ndarray, Optional[np.ndarray]]:
    im = Image.open(BytesIO(data))
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA") else im.convert("RGB")
    arr = np.array(im)
    if arr.ndim == 3 and arr.shape[2] == 4:
        rgb = arr[:, :, :3]
        a = arr[:, :, 3]
        return rgb, a
    return arr, None


def _merge_alpha(rgb: np.ndarray, alpha: Optional[np.ndarray]) -> Image.Image:
    if alpha is None:
        return Image.fromarray(rgb, mode="RGB")
    rgba = np.dstack([rgb, alpha])
    return Image.fromarray(rgba, mode="RGBA")


@router.post("/process/denoise-images")
async def denoise_images(
    files: List[UploadFile] = File(...),
    strength: float = Form(0.5),  # 0..1
    jpeg_quality: int = Form(90),
):
    if not files:
        return JSONResponse({"error": "No files provided"}, status_code=400)

    outputs: List[tuple[str, bytes, str]] = []

    for up in files:
        try:
            name = up.filename or "image"
            ext = os.path.splitext(name)[1].lower() or ".jpg"
            data = await up.read()
            rgb, alpha = _read_image_keep_alpha(data)
            
            # Use Aydin if available, otherwise fallback to OpenCV
            if AYDIN_AVAILABLE:
                logger.info(f"Using Aydin library for denoising {name}")
                out_rgb = _denoise_aydin(rgb, float(max(0.0, min(1.0, strength))))
            else:
                logger.info(f"Using OpenCV fallback for denoising {name}")
                out_rgb = _denoise_cv2(rgb, float(max(0.0, min(1.0, strength))))

            out_img = _merge_alpha(out_rgb, alpha)

            buf = BytesIO()
            mime = "image/png" if out_img.mode == "RGBA" else "image/jpeg"
            if mime == "image/png":
                out_img.save(buf, format="PNG", optimize=True, compress_level=6)
                fname = os.path.splitext(name)[0] + ".png"
            else:
                # Save as JPEG with user quality
                if out_img.mode != "RGB":
                    out_img = out_img.convert("RGB")
                out_img.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
                fname = os.path.splitext(name)[0] + ".jpg"
            buf.seek(0)
            outputs.append((fname, buf.getvalue(), mime))
        except Exception as e:
            return JSONResponse({"error": f"Failed to process {up.filename}: {e}"}, status_code=400)

    if len(outputs) == 1:
        fname, data, mime = outputs[0]
        return StreamingResponse(BytesIO(data), media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})

    # Zip for multi
    import zipfile

    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for fname, data, _ in outputs:
            z.writestr(fname, data)
    zip_buf.seek(0)
    return StreamingResponse(zip_buf, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=denoised_images.zip"})
