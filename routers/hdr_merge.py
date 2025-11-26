from __future__ import annotations

from typing import List
from io import BytesIO
import os
import tempfile

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image
try:
    import HDRutils  # type: ignore
except Exception:
    HDRutils = None

router = APIRouter()


def _read_image_rgb_float32(data: bytes) -> np.ndarray:
    im = Image.open(BytesIO(data)).convert("RGB")
    arr = np.array(im).astype(np.float32) / 255.0
    return arr


def _resize_images_to_common_size(images: List[np.ndarray]) -> List[np.ndarray]:
    """Resize all images to the smallest common dimensions to ensure compatibility."""
    if not images:
        return images
    
    # Find the minimum dimensions across all images
    min_height = min(img.shape[0] for img in images)
    min_width = min(img.shape[1] for img in images)
    
    resized_images = []
    for img in images:
        if img.shape[0] != min_height or img.shape[1] != min_width:
            # Convert to uint8 for OpenCV resize, then back to float32
            img_uint8 = (img * 255).astype(np.uint8)
            resized_uint8 = cv2.resize(img_uint8, (min_width, min_height), interpolation=cv2.INTER_LANCZOS4)
            resized_float = resized_uint8.astype(np.float32) / 255.0
            resized_images.append(resized_float)
        else:
            resized_images.append(img)
    
    return resized_images


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
    merge = cv2.createMergeMertens(contrast_weight=float(cw), saturation_weight=float(sw), exposure_weight=float(ew))
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


def _normalize_hdr(img: np.ndarray) -> np.ndarray:
    # Robustly normalize potentially HDR values into [0,1] using the 99th percentile
    x = img.astype(np.float32)
    # Guard against NaNs/Infs
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    p99 = float(np.percentile(x, 99.0)) if x.size else 1.0
    if p99 <= 1e-8:
        p99 = 1.0
    x = x / p99
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
    estimate_exp: str = Form(""),
    output_format: str = Form("jpeg"),  # 'jpeg' | 'png' | 'exr'
):
    try:
        if not files or len(files) < 2:
            return JSONResponse({"error": "Please provide at least 2 exposures of the same scene"}, status_code=400)
        names = [ (f.filename or "img") for f in files ]
        # Decide whether to use HDRutils strictly for RAW image extensions
        raw_exts = {'.arw', '.nef', '.cr2', '.cr3', '.rw2', '.dng', '.orf', '.sr2', '.raf', '.pef', '.nrw', '.rw1'}
        exts = [ os.path.splitext(n)[1].lower() for n in names ]
        all_raw = len(exts) > 0 and all((e in raw_exts) for e in exts if e)

        use_hdrutils = (HDRutils is not None) and all_raw
        file_bytes: List[bytes] = []
        if use_hdrutils:
            tmpdir = tempfile.mkdtemp(prefix="hdrutils_")
            paths = []
            try:
                for i, f in enumerate(files):
                    try:
                        # reset stream if previously read
                        if hasattr(f, 'file'):
                            f.file.seek(0)
                    except Exception:
                        pass
                    data = await f.read()
                    file_bytes.append(data)
                    name = f.filename or f"img_{i}.png"
                    base, ext = os.path.splitext(name)
                    ext = ext if ext else ".png"
                    p = os.path.join(tmpdir, f"{base}_{i}{ext}")
                    with open(p, "wb") as fp:
                        fp.write(data)
                    paths.append(p)
                kwargs = {"align": bool(align)}
                if estimate_exp:
                    kwargs["estimate_exp"] = estimate_exp
                hdr_img = HDRutils.merge(paths, **kwargs)[0]
                raw_hdr = hdr_img.astype(np.float32)
                merged = _normalize_hdr(raw_hdr)
                if float(merged.mean()) < 1e-4:
                    # Fallback if normalization still yields near-black
                    use_hdrutils = False
            except Exception:
                use_hdrutils = False
            finally:
                try:
                    for p in paths:
                        if os.path.exists(p):
                            os.remove(p)
                    if os.path.isdir(tmpdir):
                        os.rmdir(tmpdir)
                except Exception:
                    pass
        if not use_hdrutils:
            # Read all images (RGB float32 0..1)
            imgs: List[np.ndarray] = []
            if file_bytes and len(file_bytes) == len(files):
                imgs = [ _read_image_rgb_float32(b) for b in file_bytes ]
            else:
                for f in files:
                    try:
                        if hasattr(f, 'file'):
                            f.file.seek(0)
                    except Exception:
                        pass
                    data = await f.read()
                    imgs.append(_read_image_rgb_float32(data))
            imgs = _resize_images_to_common_size(imgs)
            if align:
                imgs = _align_images(imgs)
            merged = _merge_mertens(imgs, contrast_weight, saturation_weight, well_exposedness_weight)

        base = (names[0] or "hdr").rsplit('.', 1)[0]
        fmt = output_format.lower().strip()
        if fmt == "exr":
            if HDRutils is None:
                return JSONResponse({"error": "EXR output requires HDRutils and FreeImage plugin"}, status_code=400)
            tmpdir = tempfile.mkdtemp(prefix="hdrutils_out_")
            out_path = os.path.join(tmpdir, f"{base}_hdr.exr")
            try:
                # Write the raw HDR image when available; otherwise write normalized LDR
                to_write = locals().get('raw_hdr', None)
                HDRutils.imwrite(out_path, to_write if isinstance(to_write, np.ndarray) else merged)
                with open(out_path, "rb") as fp:
                    data = fp.read()
                buf = BytesIO(data)
                buf.seek(0)
                headers = {"Content-Disposition": f"attachment; filename={base}_hdr.exr"}
                return StreamingResponse(buf, media_type="image/exr", headers=headers)
            finally:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    os.rmdir(tmpdir)
                except Exception:
                    pass
        else:
            # Tone map to a nice-looking 8-bit image
            tonemapped = _tone_map(merged, tone_gamma, tone_saturation)
            out_u8 = (np.clip(tonemapped, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

            out = Image.fromarray(out_u8, mode="RGB")
            buf = BytesIO()
            if fmt == "png":
                out.save(buf, format="PNG")
                media_type = "image/png"
                fname = f"{base}_hdr.png"
            else:
                out.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
                media_type = "image/jpeg"
                fname = f"{base}_hdr.jpg"
            buf.seek(0)
            return StreamingResponse(buf, media_type=media_type, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
