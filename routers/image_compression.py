from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from typing import List
from io import BytesIO
from PIL import Image
import zipfile
import os

router = APIRouter()


def _compress_jpeg(img: Image.Image, quality: int) -> bytes:
    output = BytesIO()
    exif = img.info.get("exif")
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    save_kwargs = {
        "format": "JPEG",
        "quality": int(max(1, min(95, quality))),
        "optimize": True,
        "progressive": True,
    }
    if exif is not None:
        save_kwargs["exif"] = exif
    img.save(output, **save_kwargs)
    output.seek(0)
    return output.getvalue()

def _compress_webp(img: Image.Image, quality: int) -> bytes:
    output = BytesIO()
    img.save(
        output,
        format="WEBP",
        quality=int(max(1, min(100, quality))),
        method=6,
    )
    output.seek(0)
    return output.getvalue()

def _compress_tiff(img: Image.Image) -> bytes:
    output = BytesIO()
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    img.save(output, format="TIFF", compression="tiff_deflate")
    output.seek(0)
    return output.getvalue()


def _compress_png(img: Image.Image, compress_level: int) -> bytes:
    output = BytesIO()
    img.save(
        output,
        format="PNG",
        optimize=True,
        compress_level=int(max(0, min(9, compress_level))),
    )
    output.seek(0)
    return output.getvalue()


@router.post("/process/compress-images")
async def compress_images(
    files: List[UploadFile] = File(...),
    jpeg_quality: int = Form(85),
    png_compress_level: int = Form(6),
):
    if not files:
        return JSONResponse({"error": "No files provided"}, status_code=400)

    outputs: List[tuple[str, bytes, str]] = []  # (filename, data, mime)

    for up in files:
        try:
            name = up.filename or "image"
            ext = os.path.splitext(name)[1].lower() or ".jpg"
            data = await up.read()
            img = Image.open(BytesIO(data))
            if ext in (".jpg", ".jpeg"):
                buf = _compress_jpeg(img, jpeg_quality)
                outputs.append((os.path.splitext(name)[0] + ".jpg", buf, "image/jpeg"))
            elif ext == ".png":
                buf = _compress_png(img, png_compress_level)
                outputs.append((os.path.splitext(name)[0] + ".png", buf, "image/png"))
            elif ext == ".webp":
                buf = _compress_webp(img, jpeg_quality)
                outputs.append((os.path.splitext(name)[0] + ".webp", buf, "image/webp"))
            elif ext in (".tif", ".tiff"):
                buf = _compress_tiff(img)
                outputs.append((os.path.splitext(name)[0] + ".tiff", buf, "image/tiff"))
            elif ext == ".bmp":
                # Convert BMP to PNG for better compression
                buf = _compress_png(img, png_compress_level)
                outputs.append((os.path.splitext(name)[0] + ".png", buf, "image/png"))
            else:
                # Fallback: convert to JPEG
                buf = _compress_jpeg(img, jpeg_quality)
                outputs.append((os.path.splitext(name)[0] + ".jpg", buf, "image/jpeg"))
        except Exception as e:
            return JSONResponse({"error": f"Failed to process {up.filename}: {e}"}, status_code=400)

    if len(outputs) == 1:
        fname, data, mime = outputs[0]
        return StreamingResponse(BytesIO(data), media_type=mime, headers={
            "Content-Disposition": f"attachment; filename={fname}"
        })

    # Multiple files: create ZIP
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for fname, data, _mime in outputs:
            z.writestr(fname, data)
    zip_buf.seek(0)
    return StreamingResponse(zip_buf, media_type="application/zip", headers={
        "Content-Disposition": "attachment; filename=compressed_images.zip"
    })
