from typing import List, Optional
import io
import os
from datetime import datetime as _dt

from fastapi import APIRouter, Request, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image
import zipfile

from core.config import MAX_FILES, logger

# SECURITY: File magic bytes for image validation
IMAGE_MAGIC_BYTES = {
    b'\xff\xd8\xff': 'image/jpeg',  # JPEG
    b'\x89PNG\r\n\x1a\n': 'image/png',  # PNG
    b'RIFF': 'image/webp',  # WebP (partial - also check for WEBP)
    b'GIF87a': 'image/gif',  # GIF87a
    b'GIF89a': 'image/gif',  # GIF89a
    b'II*\x00': 'image/tiff',  # TIFF (little-endian)
    b'MM\x00*': 'image/tiff',  # TIFF (big-endian)
}

def _validate_image_content(data: bytes) -> bool:
    """Validate that file content matches expected image magic bytes."""
    if not data or len(data) < 8:
        return False
    for magic, _ in IMAGE_MAGIC_BYTES.items():
        if data[:len(magic)] == magic:
            return True
    # Additional WebP check (RIFF....WEBP)
    if data[:4] == b'RIFF' and len(data) > 11 and data[8:12] == b'WEBP':
        return True
    # HEIC/HEIF check (ftyp box)
    if len(data) > 11 and data[4:8] == b'ftyp':
        return True
    return False
from core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from utils.watermark import (
    add_text_watermark,
    add_signature_watermark,
    add_text_watermark_tiled,
    add_signature_watermark_tiled,
)
from utils.storage import upload_bytes, read_json_key
from utils.invisible_mark import embed_signature as embed_invisible, build_payload_for_uid
from utils.metadata import auto_embed_metadata_for_user
from utils.rate_limit import (
    check_upload_rate_limit,
    validate_upload_request,
    validate_file_size,
    MAX_FILE_SIZE_BYTES,
    MAX_FILES_PER_BATCH,
)
from sqlalchemy.orm import Session
from core.database import get_db
from models.gallery import GalleryAsset

# Import vault helpers to update vaults after upload
from routers.vaults import (
    _read_vault, _write_vault, _vault_key,
    _read_vault_meta, _write_vault_meta, _unlock_vault,
    _vault_salt, _hash_password_bcrypt
)

# Import cloud storage auto-sync triggers
try:
    from routers.google_drive import trigger_auto_sync_if_enabled as gdrive_auto_sync
except ImportError:
    gdrive_auto_sync = None

try:
    from routers.dropbox import trigger_auto_sync_if_enabled as dropbox_auto_sync
except ImportError:
    dropbox_auto_sync = None

try:
    from routers.onedrive import trigger_auto_sync_if_enabled as onedrive_auto_sync
except ImportError:
    onedrive_auto_sync = None

router = APIRouter(prefix="", tags=["upload"])  # no prefix to serve /upload


@router.post("/upload")
async def upload(
    request: Request,
    files: List[UploadFile] = File(...),
    watermark: Optional[str] = Form(None),
    wm_pos: str = Form("bottom-right"),
    signature: Optional[UploadFile] = File(None),  # legacy field name
    logo: Optional[UploadFile] = File(None),       # new preferred field name
    wm_color: Optional[str] = Form(None),
    wm_opacity: Optional[float] = Form(None),
    # Visible layout mode and tiling params
    wm_layout: Optional[str] = Form("single"),
    tile_angle: Optional[float] = Form(None),
    tile_spacing: Optional[float] = Form(None),
    tile_scale: Optional[float] = Form(None),
    # Single-layout background box flag
    wm_bg_box: Optional[str] = Form(None),  # '1' to enable background box on single watermark
    wm_text_rel: Optional[float] = Form(None),
    wm_logo_rel: Optional[float] = Form(None),
    wm_logo_size: Optional[int] = Form(None),
    artist: Optional[str] = Form(None),
    invisible: Optional[str] = Form(None),  # '1' to embed invisible signature
    # Destination options
    vault_mode: str = Form("all"),  # 'all' | 'existing' | 'new'
    vault_name: Optional[str] = Form(None),
    vault_protect: Optional[str] = Form(None),  # '1' to protect new vault
    vault_password: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Upload writes to user's watermarked area; allowed for admin and retoucher roles
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)
    
    # Validate batch size limits
    valid, err_msg = validate_upload_request(len(files), 0)
    if not valid:
        return JSONResponse({"error": err_msg}, status_code=400)
    
    # Check rate limits before processing
    allowed, rate_err = check_upload_rate_limit(uid, file_count=len(files))
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)

    # Read logo/signature if provided (support both for backward compatibility)
    logo_file = logo or signature
    logo_bytes = await logo_file.read() if logo_file is not None else None
    use_logo = bool(logo_bytes)

    # Validate text mode
    if not use_logo and not (watermark or '').strip():
        return JSONResponse({"error": "watermark text required or provide logo"}, status_code=400)

    uploaded = []

    idx = 0
    total_size = 0
    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            
            # SECURITY: Validate individual file size
            file_valid, file_err = validate_file_size(len(raw), uf.filename or '')
            if not file_valid:
                logger.warning(f"[upload] File too large: {uf.filename} ({len(raw)} bytes)")
                continue
            
            total_size += len(raw)
            
            # SECURITY: Validate file content matches image magic bytes
            if not _validate_image_content(raw):
                logger.warning(f"[upload] Invalid image content for {getattr(uf, 'filename', 'unknown')}")
                continue
            
            img = Image.open(io.BytesIO(raw)).convert("RGB")

            # Determine original file extension and content-type
            orig_ext = (os.path.splitext(uf.filename or '')[1] or '.jpg').lower()
            # Normalize some odd cases
            if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'):
                orig_ext = orig_ext if len(orig_ext) <= 6 and orig_ext.startswith('.') else '.bin'
            ct_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.bin': 'application/octet-stream'
            }
            orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

            # Build watermark (supports single or tiled layout)
            layout = (wm_layout or 'single').strip().lower()
            if use_logo:
                sig = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")  # type: ignore[arg-type]
                if layout == 'tiled':
                    out = add_signature_watermark_tiled(
                        img,
                        sig,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_signature_watermark(
                        img,
                        sig,
                        wm_pos,
                        bg_box=((wm_bg_box or '').strip() == '1'),
                        target_w_override=int(wm_logo_size) if wm_logo_size is not None else None,
                        rel_w=float(wm_logo_rel) if wm_logo_rel is not None else None
                    )
            else:
                if layout == 'tiled':
                    out = add_text_watermark_tiled(
                        img,
                        watermark or '',
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_text_watermark(
                        img,
                        watermark or '',
                        wm_pos,
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        bg_box=((wm_bg_box or '').strip() == '1'),
                        base_size_rel=float(wm_text_rel) if wm_text_rel is not None else None
                    )

            # Optionally embed invisible signature linked to the account uid
            try:
                if (invisible or '').strip() == '1':
                    payload = build_payload_for_uid(uid)
                    out = embed_invisible(out, payload)
            except Exception as _ex:
                logger.warning(f"invisible embed failed: {_ex}")

            # Encode watermarked JPEG with optional EXIF metadata
            buf = io.BytesIO()
            try:
                # Check if user has auto-embed metadata enabled
                from utils.metadata import MetadataSettings, embed_metadata
                meta_data = read_json_key(f"users/{uid}/settings/metadata.json") or {}
                auto_embed = bool(meta_data.get("auto_embed", False))
                
                if auto_embed and meta_data.get("photographer_name"):
                    # Use full metadata settings
                    settings = MetadataSettings.from_dict(meta_data)
                    _, jpeg_bytes = embed_metadata(out, settings)
                    buf.write(jpeg_bytes)
                else:
                    # Fallback to basic artist field
                    import piexif  # type: ignore
                    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                    if (artist or '').strip():
                        exif_dict["0th"][piexif.ImageIFD.Artist] = artist  # type: ignore[attr-defined]
                    exif_bytes = piexif.dump(exif_dict)
                    out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
            except Exception:
                out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
            buf.seek(0)

            date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
            base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
            stamp = int(_dt.utcnow().timestamp())
            suffix = 'logo' if use_logo else 'txt'

            # Upload only the WATERMARKED jpeg (no original to save storage and bandwidth)
            key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-{suffix}.jpg"
            data = buf.getvalue()
            url = upload_bytes(key, data, content_type='image/jpeg')

            uploaded.append({"key": key, "url": url})
            idx += 1
            try:
                existing = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                if existing:
                    existing.user_uid = uid
                    existing.vault = None
                    existing.size_bytes = len(data)
                else:
                    db.add(GalleryAsset(user_uid=uid, vault=None, key=key, size_bytes=len(data)))
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        except Exception as ex:
            logger.warning(f"upload failed for {getattr(uf,'filename', '')}: {ex}")
            continue

    # Vault handling
    final_vault = None
    try:
        vm = (vault_mode or 'all').strip().lower()
        if vm == 'existing':
            name = (vault_name or '').strip()
            if name and uploaded:
                exist = _read_vault(uid, name)
                merged = sorted(set(exist) | {u['key'] for u in uploaded})
                _write_vault(uid, name, merged)
                final_vault = _vault_key(uid, name)[1]
        elif vm == 'new':
            name = (vault_name or '').strip()
            if name:
                keys_now = [u['key'] for u in uploaded]
                _write_vault(uid, name, keys_now)
                prot = (vault_protect or '').strip() == '1'
                if prot and (vault_password or '').strip():
                    _write_vault_meta(uid, name, {"protected": True, "password_hash": _hash_password_bcrypt(vault_password or '')})
                final_vault = _vault_key(uid, name)[1]
    except Exception as ex:
        logger.warning(f"vault update failed: {ex}")

    return {"ok": True, "uploaded": uploaded, "vault": final_vault}


@router.post("/api/uploads")
async def upload_external(
    request: Request,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Upload raw user photos (no watermark) into users/{uid}/external/.
    - Requires gallery access for the effective workspace.
    - Stores files under users/{uid}/external/YYYY/MM/DD/base-stamp.ext
    - Returns list of uploaded items with keys and public URLs.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Gallery managers/owners can upload into their own external area
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    
    # Rate limiting and validation - check if batch contains videos for appropriate limits
    from utils.rate_limit import is_video_file
    has_videos = any(is_video_file(f.filename or '') for f in files)
    valid, err_msg = validate_upload_request(len(files), 0, has_videos=has_videos)
    if not valid:
        return JSONResponse({"error": err_msg}, status_code=400)
    
    allowed, rate_err = check_upload_rate_limit(uid, file_count=len(files))
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)

    # Dynamic per-plan cap: individual/studios get higher limit if configured
    max_cap = MAX_FILES
    try:
        ent = read_json_key(f"users/{uid}/billing/entitlement.json") or {}
        plan = str(ent.get("plan") or "").lower()
        is_paid = bool(ent.get("isPaid") or False)
        import os
        paid_cap = int(os.getenv("UPLOAD_MAX_PAID", "1000"))
        free_cap = int(os.getenv("UPLOAD_MAX_FREE", str(MAX_FILES)))
        # Normalize known paid plans to get 1000 cap by default (includes backward compatibility)
        if is_paid and ("individual" in plan or "studio" in plan or "agenc" in plan or "photograph" in plan):
            max_cap = max(MAX_FILES, min(paid_cap, 5000))
        else:
            max_cap = free_cap if not is_paid else min(paid_cap, 5000)
    except Exception:
        max_cap = MAX_FILES

    if len(files) > max_cap:
        return JSONResponse({"error": f"too many files (max {max_cap})"}, status_code=400)

    uploaded = []
    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            
            # SECURITY: Validate file size
            file_valid, file_err = validate_file_size(len(raw), uf.filename or '')
            if not file_valid:
                logger.warning(f"[upload.external] File too large: {uf.filename} ({len(raw)} bytes)")
                continue
            
            # SECURITY: Validate file content matches image magic bytes
            if not _validate_image_content(raw):
                logger.warning(f"[upload.external] Invalid image content for {getattr(uf, 'filename', 'unknown')}")
                continue
            
            # Determine original file extension and content-type
            orig_ext = (os.path.splitext(uf.filename or '')[1] or '.jpg').lower()
            if not orig_ext.startswith('.') or len(orig_ext) > 8:
                orig_ext = '.jpg'
            # Normalize some odd cases
            if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff', '.gif'):
                orig_ext = '.jpg'
            ct_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.gif': 'image/gif'
            }
            orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

            # Auto-embed IPTC/EXIF metadata if user has it enabled
            try:
                raw = auto_embed_metadata_for_user(raw, uid)
            except Exception as meta_ex:
                logger.debug(f"Metadata embed skipped: {meta_ex}")

            date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
            base = os.path.splitext(os.path.basename(uf.filename or 'upload'))[0] or 'upload'
            stamp = int(_dt.utcnow().timestamp())
            key = f"users/{uid}/external/{date_prefix}/{base}-{stamp}{orig_ext}"
            url = upload_bytes(key, raw, content_type=orig_ct)
            uploaded.append({"key": key, "url": url, "name": os.path.basename(key)})
            try:
                existing = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
                if existing:
                    existing.user_uid = uid
                    existing.vault = None
                    existing.size_bytes = len(raw)
                else:
                    db.add(GalleryAsset(user_uid=uid, vault=None, key=key, size_bytes=len(raw)))
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        except Exception as ex:
            logger.warning(f"external upload failed for {getattr(uf,'filename', '')}: {ex}")
            continue

    # Trigger cloud storage auto-sync if enabled
    if uploaded:
        uploaded_keys = [u["key"] for u in uploaded]
        
        # Google Drive auto-sync
        if gdrive_auto_sync is not None:
            try:
                await gdrive_auto_sync(uid, uploaded_keys)
            except Exception as sync_ex:
                logger.warning(f"Google Drive auto-sync trigger failed: {sync_ex}")
        
        # Dropbox auto-sync
        if dropbox_auto_sync is not None:
            try:
                await dropbox_auto_sync(uid, uploaded_keys)
            except Exception as sync_ex:
                logger.warning(f"Dropbox auto-sync trigger failed: {sync_ex}")
        
        # OneDrive auto-sync
        if onedrive_auto_sync is not None:
            try:
                await onedrive_auto_sync(uid, uploaded_keys)
            except Exception as sync_ex:
                logger.warning(f"OneDrive auto-sync trigger failed: {sync_ex}")

    return {"ok": True, "uploaded": uploaded}


# New: process and return a ZIP without uploading anywhere
@router.post("/process/watermark-zip")
async def process_watermark_zip(
    request: Request,
    files: List[UploadFile] = File(...),
    watermark: Optional[str] = Form(None),
    wm_pos: str = Form("bottom-right"),
    signature: Optional[UploadFile] = File(None),  # legacy
    logo: Optional[UploadFile] = File(None),       # preferred
    wm_color: Optional[str] = Form(None),
    wm_opacity: Optional[float] = Form(None),
    wm_layout: Optional[str] = Form("single"),
    tile_angle: Optional[float] = Form(None),
    tile_spacing: Optional[float] = Form(None),
    tile_scale: Optional[float] = Form(None),
    wm_bg_box: Optional[str] = Form(None),
    wm_text_rel: Optional[float] = Form(None),
    wm_logo_rel: Optional[float] = Form(None),
    wm_logo_size: Optional[int] = Form(None),
    artist: Optional[str] = Form(None),
    invisible: Optional[str] = Form(None),
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    
    # Rate limiting and validation
    valid, err_msg = validate_upload_request(len(files), 0)
    if not valid:
        return JSONResponse({"error": err_msg}, status_code=400)
    
    allowed, rate_err = check_upload_rate_limit(uid, file_count=len(files))
    if not allowed:
        return JSONResponse({"error": rate_err}, status_code=429)

    # Read logo/signature if provided
    logo_file = logo or signature
    logo_bytes = await (logo_file.read() if logo_file is not None else None)
    use_logo = bool(logo_bytes)

    if not use_logo and not (watermark or '').strip():
        return JSONResponse({"error": "watermark text required or provide logo"}, status_code=400)

    # Helper to process a single file and return (filename, jpeg_bytes)
    async def _process_one(uf: UploadFile) -> Optional[tuple[str, bytes]]:
        try:
            raw = await uf.read()
            if not raw:
                return None
            
            # Validate file size
            file_valid, _ = validate_file_size(len(raw), uf.filename or '')
            if not file_valid:
                return None
            
            img = Image.open(io.BytesIO(raw)).convert("RGB")

            layout = (wm_layout or 'single').strip().lower()
            if use_logo:
                sig = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")  # type: ignore[arg-type]
                if layout == 'tiled':
                    out = add_signature_watermark_tiled(
                        img,
                        sig,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_signature_watermark(
                        img,
                        sig,
                        wm_pos,
                        bg_box=((wm_bg_box or '').strip() == '1'),
                        target_w_override=int(wm_logo_size) if wm_logo_size is not None else None,
                        rel_w=float(wm_logo_rel) if wm_logo_rel is not None else None
                    )
            else:
                if layout == 'tiled':
                    out = add_text_watermark_tiled(
                        img,
                        watermark or '',
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_text_watermark(
                        img,
                        watermark or '',
                        wm_pos,
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        bg_box=((wm_bg_box or '').strip() == '1'),
                        base_size_rel=float(wm_text_rel) if wm_text_rel is not None else None
                    )

            # Optional invisible signature
            try:
                if (invisible or '').strip() == '1':
                    payload = build_payload_for_uid(uid)
                    out = embed_invisible(out, payload)
            except Exception as _ex:
                logger.warning(f"invisible embed (zip) failed: {_ex}")

            # Encode JPEG with optional EXIF metadata
            buf = io.BytesIO()
            try:
                # Check if user has auto-embed metadata enabled
                from utils.metadata import MetadataSettings, embed_metadata
                meta_data = read_json_key(f"users/{uid}/settings/metadata.json") or {}
                auto_embed = bool(meta_data.get("auto_embed", False))
                
                if auto_embed and meta_data.get("photographer_name"):
                    # Use full metadata settings
                    settings = MetadataSettings.from_dict(meta_data)
                    _, jpeg_bytes = embed_metadata(out, settings)
                    buf.write(jpeg_bytes)
                else:
                    # Fallback to basic artist field
                    import piexif  # type: ignore
                    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                    if (artist or '').strip():
                        exif_dict["0th"][piexif.ImageIFD.Artist] = artist  # type: ignore[attr-defined]
                    exif_bytes = piexif.dump(exif_dict)
                    out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
            except Exception:
                out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
            buf.seek(0)

            base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
            name = f"{base}-watermarked.jpg"
            return (name, buf.getvalue())
        except Exception as ex:
            logger.warning(f"zip process failed for {getattr(uf,'filename','')}: {ex}")
            return None

    # If only one file, return the single JPEG directly
    if len(files) == 1:
        one = await _process_one(files[0])
        if not one:
            return JSONResponse({"error": "processing failed"}, status_code=400)
        name, data = one
        headers = { 'Content-Disposition': f'attachment; filename="{name}"' }
        return StreamingResponse(io.BytesIO(data), media_type='image/jpeg', headers=headers)

    # Otherwise, build a ZIP with manifest
    # Process all files FIRST (outside the zipfile context) to avoid async issues
    processed: list[tuple[str, str, bytes]] = []  # (orig_name, output_name, data)
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
        name, data = res
        final_name = _unique_name(name)
        orig = os.path.basename(uf.filename or '') or 'image.jpg'
        processed.append((orig, final_name, data))
    
    # Now create the ZIP synchronously with all data ready
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for orig, final_name, data in processed:
            zf.writestr(final_name, data)
        # Write manifest
        try:
            if processed:
                lines = ["Original Filename -> Output Filename"] + [f"{o} -> {n}" for (o, n, _) in processed]
                zf.writestr('manifest.txt', "\n".join(lines))
        except Exception:
            pass

    mem.seek(0)
    stamp = _dt.utcnow().strftime('%Y%m%d-%H%M%S')
    headers = {
        'Content-Disposition': f'attachment; filename="watermarked-{stamp}.zip"'
    }
    return StreamingResponse(mem, media_type='application/zip', headers=headers)
