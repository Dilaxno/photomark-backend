from __future__ import annotations

from typing import List
from io import BytesIO

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image

router = APIRouter()


def _read_image_rgb_float32(data: bytes) -> np.ndarray:
    im = Image.open(BytesIO(data)).convert("RGB")
    arr = np.array(im).astype(np.float32) / 255.0
    return arr


def _align_images(images: List[np.ndarray]) -> List[np.ndarray]:
    # OpenCV AlignMTB works on 8-bit images in BGR
    bgrs = [cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2BGR) for img in images]
    align = cv2.createAlignMTB()
    align.process(bgrs, bgrs)  # in-place alignment
    rgbs = [cv2.cvtColor(im, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0 for im in bgrs]
    return rgbs


def _merge_mertens(images: List[np.ndarray], cw: float, sw: float, ew: float) -> np.ndarray:
    # Expect float32 RGB in [0,1]
    bgrs = [cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2BGR) for img in images]
    # MergeMertens requires float inputs in [0,1]
    bgrs_f = [im.astype(np.float32) / 255.0 for im in bgrs]
    merge = cv2.createMergeMertens(contrast_weight=float(cw), saturation_weight=float(sw), exposedness_weight=float(ew))
    fusion = merge.process(bgrs_f)  # float32 BGR [0,1]
    rgb = cv2.cvtColor((fusion * 255.0).astype(np.uint8), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return rgb


def _tone_map(img: np.ndarray, gamma: float, saturation: float) -> np.ndarray:
    # Apply simple gamma correction and optional saturation boost in HSV
    # img: float32 RGB [0,1]
    x = np.clip(img, 0.0, 1.0)
    if gamma and gamma > 0:
        x = np.power(x, 1.0 / float(gamma))
    # saturation in 0..2 (1=none)
    if saturation and abs(saturation - 1.0) > 1e-3:
        hsv = cv2.cvtColor((x * 255).astype(np.uint8), cv2.COLOR_RGB2HSV).astype(np.float32)
        hsv[...,1] = np.clip(hsv[...,1] * float(saturation), 0, 255)
        x = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB).astype(np.float32) / 255.0
    return np.clip(x, 0.0, 1.0)


@router.post("/process/hdr-merge")
async def hdr_merge(
    files: List[UploadFile] = File(...),
    align: bool = Form(True),
    contrast_weight: float = Form(1.0),
    saturation_weight: float = Form(1.0),
    well_exposedness_weight: float = Form(1.0),
    tone_gamma: float = Form(1.2),
    tone_saturation: float = Form(1.0),
    jpeg_quality: int = Form(95),
):
    try:
        if not files or len(files) < 2:
            return JSONResponse({"error": "Please provide at least 2 exposures of the same scene"}, status_code=400)

        # Read all images (RGB float32 0..1)
        imgs = []
        names = []
        for f in files:
            names.append(f.filename or "img")
            data = await f.read()
            imgs.append(_read_image_rgb_float32(data))

        # Align slight motion between frames if requested
        if align:
            imgs = _align_images(imgs)

        # Merge using Mertens exposure fusion
        merged = _merge_mertens(imgs, contrast_weight, saturation_weight, well_exposedness_weight)

        # Tone map to a nice-looking 8-bit image
        tonemapped = _tone_map(merged, tone_gamma, tone_saturation)
        out_u8 = (np.clip(tonemapped, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

        # Encode to JPEG
        out = Image.fromarray(out_u8, mode="RGB")
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
        buf.seek(0)
        # Name from first file
        base = (names[0] or "hdr").rsplit('.', 1)[0]
        fname = f"{base}_hdr.jpg"
        return StreamingResponse(buf, media_type="image/jpeg", headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
