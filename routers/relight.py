from __future__ import annotations

import os
from io import BytesIO
from typing import List

import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image

router = APIRouter()

MODEL = None
MODEL_ERR = None

def _ensure_model():
  global MODEL, MODEL_ERR
  if MODEL is not None or MODEL_ERR is not None:
    return MODEL
  try:
    from huggingface_hub import from_pretrained_keras
    MODEL = from_pretrained_keras("keras-io/low-light-image-enhancement")
    return MODEL
  except Exception as ex:
    MODEL_ERR = str(ex)
    return None

def _enhance_with_model(img: Image.Image, strength: float) -> Image.Image:
  m = _ensure_model()
  if m is None:
    raise RuntimeError(MODEL_ERR or "Model unavailable")
  arr = np.array(img.convert("RGB"), dtype=np.float32) / 255.0
  h, w = img.size[1], img.size[0]
  inp = Image.fromarray((arr * 255.0).astype(np.uint8))
  inp = inp.resize((256, 256), Image.BICUBIC)
  x = np.array(inp, dtype=np.float32) / 255.0
  x = np.expand_dims(x, 0)
  y = m.predict(x, verbose=0)
  y0 = y[0]
  out_small = (np.clip(y0 * 255.0 + 0.5, 0, 255)).astype(np.uint8)
  out_img = Image.fromarray(out_small, mode="RGB")
  out_img = out_img.resize((w, h), Image.BICUBIC)
  if 0.0 <= strength <= 1.0:
    base = np.array(img.convert("RGB"), dtype=np.float32)
    enh = np.array(out_img, dtype=np.float32)
    mix = (1.0 - strength) * base + strength * enh
    mix = np.clip(mix + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(mix, mode="RGB")
  return out_img

def _enhance_fallback(img: Image.Image, strength: float) -> Image.Image:
  import cv2
  rgb = np.array(img.convert("RGB"))
  lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
  l, a, b = cv2.split(lab)
  clahe = cv2.createCLAHE(clipLimit=2.0 + max(0.0, min(1.0, strength)) * 2.0, tileGridSize=(8,8))
  l2 = clahe.apply(l)
  lab2 = cv2.merge((l2, a, b))
  rgb2 = cv2.cvtColor(lab2, cv2.COLOR_LAB2RGB)
  return Image.fromarray(rgb2, mode="RGB")

@router.post("/process/relight-images")
async def relight_images(
  files: List[UploadFile] = File(...),
  strength: float = Form(0.75),
  jpeg_quality: int = Form(92),
):
  if not files:
    return JSONResponse({"error": "No files provided"}, status_code=400)
  outputs: List[tuple[str, bytes, str]] = []
  for up in files:
    try:
      name = up.filename or "image"
      data = await up.read()
      img = Image.open(BytesIO(data))
      try:
        out = _enhance_with_model(img, float(max(0.0, min(1.0, strength))))
      except Exception:
        out = _enhance_fallback(img, float(max(0.0, min(1.0, strength))))
      buf = BytesIO()
      if out.mode != "RGB":
        out = out.convert("RGB")
      out.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
      buf.seek(0)
      fname = os.path.splitext(name)[0] + "_relight.jpg"
      outputs.append((fname, buf.getvalue(), "image/jpeg"))
    except Exception as ex:
      return JSONResponse({"error": f"Failed to process {up.filename}: {ex}"}, status_code=400)
  if len(outputs) == 1:
    fname, data, mime = outputs[0]
    return StreamingResponse(BytesIO(data), media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
  import zipfile
  zip_buf = BytesIO()
  with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
    for fname, data, _ in outputs:
      z.writestr(fname, data)
  zip_buf.seek(0)
  return StreamingResponse(zip_buf, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=relight_batch.zip"})

