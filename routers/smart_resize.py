from typing import List, Optional, Tuple
import io
import os
from datetime import datetime as _dt

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image
import zipfile

from core.config import MAX_FILES, logger
from core.auth import resolve_workspace_uid, has_role_access
from utils.smart_crop import SmartCropper, parse_presets
from utils.storage import upload_bytes

router = APIRouter(prefix="", tags=["smart-resize"])  # public-style endpoints


_cropper = SmartCropper()


def _safe_open_image(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


@router.post("/process/smart-resize-zip")
async def process_smart_resize_zip(
    request: Request,
    files: List[UploadFile] = File(...),
    presets: Optional[str] = Form(None),  # e.g., "16x9,4x5,9x16,1000x1500,1200x628"
    quality: Optional[int] = Form(None),  # global JPEG quality 1-100
    preset_quality: Optional[str] = Form(None),  # CSV like "16x9:90,4x5:95"
):
    """
    Process a batch of images using subject-aware smart cropping and resize into requested presets.
    Returns a ZIP file of results. If only one input and one preset, returns a single JPEG for convenience.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Allow retouch role (same as watermark tools)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    prs = parse_presets(presets)

    def _parse_preset_quality(s: Optional[str]) -> dict[str, int]:
        m: dict[str, int] = {}
        if not s:
            return m
        for tok in [t.strip() for t in s.split(',') if t.strip()]:
            if ':' in tok:
                k, v = tok.split(':', 1)
                k = k.strip().lower().replace('×','x').replace(':','x').replace(' ','')
                try:
                    q = max(1, min(100, int(v.strip())))
                except Exception:
                    continue
                m[k] = q
        return m

    q_global = max(1, min(100, int(quality))) if (quality is not None) else 95
    q_map = _parse_preset_quality(preset_quality)

    async def _process_one(uf: UploadFile) -> Optional[list[tuple[str, bytes]]]:
        try:
            raw = await uf.read()
            if not raw:
                return None
            img = _safe_open_image(raw)
            base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'

            results: list[tuple[str, bytes]] = []
            for name, (w, h) in prs:
                out_img = _cropper.crop_and_resize(img, w, h)
                buf = io.BytesIO()
                # Always export JPEG for consistency
                q_use = int(q_map.get(name, q_global))
                out_img.convert("RGB").save(buf, format="JPEG", quality=q_use, subsampling=0, progressive=True, optimize=True)
                buf.seek(0)
                out_name = f"{base}-{name}.jpg"
                results.append((out_name, buf.getvalue()))
            return results
        except Exception as ex:
            logger.warning(f"smart-resize process failed for {getattr(uf,'filename','')}: {ex}")
            return None

    # Optimize single image + single preset case to return the image directly
    if len(files) == 1 and len(prs) == 1:
        res = await _process_one(files[0])
        if not res:
            return JSONResponse({"error": "processing failed"}, status_code=400)
        name, data = res[0]
        headers = { 'Content-Disposition': f'attachment; filename="{name}"' }
        return StreamingResponse(io.BytesIO(data), media_type='image/jpeg', headers=headers)

    # Build a ZIP
    mem = io.BytesIO()
    mappings: list[tuple[str, str]] = []
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        used_names: set[str] = set()
        def _unique_name(n: str) -> str:
            base, ext = os.path.splitext(n)
            cand = n
            i = 1
            while cand in used_names:
                cand = f"{base}_{i}{ext}"
                i += 1
            used_names.add(cand)
            return cand
        for uf in files:
            res = await _process_one(uf)
            if not res:
                continue
            orig = os.path.basename(uf.filename or '') or 'image.jpg'
            for (name, data) in res:
                final_name = _unique_name(name)
                mappings.append((orig, final_name))
                zf.writestr(final_name, data)
        # Write manifest
        try:
            if mappings:
                lines = ["Original Filename -> Output Filename"] + [f"{o} -> {n}" for (o, n) in mappings]
                zf.writestr('manifest.txt', "\n".join(lines))
        except Exception:
            pass

    mem.seek(0)
    stamp = _dt.utcnow().strftime('%Y%m%d-%H%M%S')
    headers = {
        'Content-Disposition': f'attachment; filename="smart-resized-{stamp}.zip"'
    }
    return StreamingResponse(mem, media_type='application/zip', headers=headers)


@router.post("/api/smart-resize-upload")
async def smart_resize_upload(
    request: Request,
    files: List[UploadFile] = File(...),
    presets: Optional[str] = Form(None),
    destination: str = Form("r2"),
    quality: Optional[int] = Form(None),
    preset_quality: Optional[str] = Form(None),
):
    """
    Process and upload resized outputs into the user's gallery.
    Writes to users/{uid}/watermarked/YYYY/MM/DD/<base>-<stamp>-<preset>-sr-o<orig>.jpg
    Returns list of uploaded file keys and URLs.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    prs = parse_presets(presets)

    uploaded = []
    date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
    stamp = int(_dt.utcnow().timestamp())

    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            img = _safe_open_image(raw)
            base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
            orig_ext = (os.path.splitext(uf.filename or '')[1] or '.jpg').lower()
            if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'):
                orig_ext = orig_ext if len(orig_ext) <= 6 and orig_ext.startswith('.') else '.bin'
            oext_token = (orig_ext.lstrip('.') or 'jpg').lower()

            for name, (w, h) in prs:
                out_img = _cropper.crop_and_resize(img, w, h)
                buf = io.BytesIO()
                q_use = int(q_map.get(name, q_global))
                out_img.convert("RGB").save(buf, format="JPEG", quality=q_use, subsampling=0, progressive=True, optimize=True)
                buf.seek(0)
                key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-{name}-sr-o{oext_token}.jpg"
                url = upload_bytes(key, buf.getvalue(), content_type='image/jpeg')
                uploaded.append({"key": key, "url": url, "preset": name})
        except Exception as ex:
            logger.warning(f"smart-resize upload failed for {getattr(uf,'filename','')}: {ex}")
            continue

    return {"ok": True, "uploaded": uploaded}
