from __future__ import annotations

import os
from io import BytesIO
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image

# Optional heavy deps
import torch
import torch.nn as nn
import cv2

router = APIRouter()


class DnCNN(nn.Module):
    def __init__(self, depth: int = 17, n_channels: int = 64, image_channels: int = 3):
        super().__init__()
        kernel_size = 3
        padding = 1
        layers = [
            nn.Conv2d(in_channels=image_channels, out_channels=n_channels, kernel_size=kernel_size, padding=padding, bias=True),
            nn.ReLU(inplace=True),
        ]
        for _ in range(depth - 2):
            layers += [
                nn.Conv2d(n_channels, n_channels, kernel_size, padding=padding, bias=False),
                nn.BatchNorm2d(n_channels),
                nn.ReLU(inplace=True),
            ]
        layers += [nn.Conv2d(in_channels=n_channels, out_channels=image_channels, kernel_size=kernel_size, padding=padding, bias=False)]
        self.dncnn = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Residual learning: output is predicted noise
        noise = self.dncnn(x)
        return x - noise


def _load_dncnn(weights_path: Optional[str]) -> Optional[DnCNN]:
    try:
        model = DnCNN()
        if weights_path and os.path.isfile(weights_path):
            state = torch.load(weights_path, map_location="cpu")
            # Support both plain state_dict and wrapped dict
            if isinstance(state, dict) and "state_dict" in state:
                state = state["state_dict"]
            # Handle keys with 'module.' prefix
            new_state = {k.replace("module.", ""): v for k, v in state.items()}
            model.load_state_dict(new_state, strict=False)
        else:
            # No weights provided -> return None to trigger fallback
            return None
        model.eval()
        return model
    except Exception:
        return None


def _denoise_dncnn(img: np.ndarray, strength: float, model: DnCNN) -> np.ndarray:
    # img: HxWxC in RGB [0..255]
    im = img.astype(np.float32) / 255.0
    # Scale input by strength using simple trick: blend with mild noise estimate
    x = torch.from_numpy(im.transpose(2, 0, 1)).unsqueeze(0)
    with torch.no_grad():
        out = model(x).clamp(0.0, 1.0)
        out_np = out.squeeze(0).cpu().numpy().transpose(1, 2, 0)
    if 0.0 <= strength <= 1.0:
        out_np = (1 - strength) * im + strength * out_np
    out_np = np.clip(out_np * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out_np


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

    # Try to load DnCNN model
    weights = os.getenv("DNCNN_WEIGHTS", "./models/dncnn.pth")
    model = _load_dncnn(weights)

    outputs: List[tuple[str, bytes, str]] = []

    for up in files:
        try:
            name = up.filename or "image"
            ext = os.path.splitext(name)[1].lower() or ".jpg"
            data = await up.read()
            rgb, alpha = _read_image_keep_alpha(data)
            if model is not None:
                out_rgb = _denoise_dncnn(rgb, float(max(0.0, min(1.0, strength))), model)
            else:
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
