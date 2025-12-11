from __future__ import annotations

import os
import logging
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

# Set up logging
logger = logging.getLogger(__name__)

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
            logger.info(f"Loading DnCNN model from: {weights_path}")
            state = torch.load(weights_path, map_location="cpu")
            # Support both plain state_dict and wrapped dict
            if isinstance(state, dict) and "state_dict" in state:
                state = state["state_dict"]
            # Handle keys with 'module.' prefix
            new_state = {k.replace("module.", ""): v for k, v in state.items()}
            model.load_state_dict(new_state, strict=False)
            logger.info("DnCNN model loaded successfully")
        else:
            logger.warning(f"DnCNN weights not found at: {weights_path}")
            return None
        model.eval()
        return model
    except Exception as e:
        logger.error(f"Failed to load DnCNN model: {e}")
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


def _denoise_cv2(
    img: np.ndarray,
    strength: float,
    h_luma: Optional[int] = None,
    h_color: Optional[int] = None,
    template_size: int = 7,
    search_size: int = 21,
    use_gray: bool = False,
) -> np.ndarray:
    hl = h_luma if h_luma is not None else int(max(0, min(50, 5 + strength * 15)))
    hc = h_color if h_color is not None else int(max(0, min(50, 5 + strength * 15)))
    ts = max(3, template_size)
    ts = ts if ts % 2 == 1 else ts + 1
    ss = max(5, search_size)
    ss = ss if ss % 2 == 1 else ss + 1
    if use_gray or (img.ndim == 2 or (img.ndim == 3 and img.shape[2] == 1)):
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
        den = cv2.fastNlMeansDenoising(gray, None, hl, ts, ss)
        rgb = cv2.cvtColor(den, cv2.COLOR_GRAY2RGB)
        return rgb
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    den = cv2.fastNlMeansDenoisingColored(bgr, None, hl, hc, ts, ss)
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


# ---------------- Frequency-domain periodic noise removal (inspired by anadi45/Image-Noise-Remover) ---------------- #
def _butterworth_lp(D0: float, shape: tuple[int, int], n: int) -> np.ndarray:
    rows, cols = shape
    y = np.arange(rows) - rows / 2.0
    x = np.arange(cols) - cols / 2.0
    X, Y = np.meshgrid(x, y)
    D = np.sqrt(X ** 2 + Y ** 2)
    # Avoid division by zero
    D = np.where(D == 0, 1e-6, D)
    H = 1.0 / (1.0 + (D / max(1.0, D0)) ** (2 * max(1, n)))
    return H.astype(np.float32)


def _apply_mask_for_noise_type(shifted: np.ndarray, noise_type: int) -> np.ndarray:
    # shifted: complex spectrum centered (fftshift) with shape (H, W, C) or (H, W)
    H, W = shifted.shape[:2]
    crow, ccol = H // 2, W // 2
    masked = shifted.copy()
    # For multi-channel, operate on each channel
    if masked.ndim == 3:
        for c in range(masked.shape[2]):
            masked[:, :, c] = _apply_mask_for_noise_type(masked[:, :, c], noise_type)
        return masked
    # Single channel masking similar to repo's logic
    if noise_type == 1:
        # horizontal band remove (vertical periodic noise in spatial domain)
        masked[crow - 4:crow + 4, 0:ccol - 10] = 1
        masked[crow - 4:crow + 4, ccol + 10:] = 1
    elif noise_type == 2:
        # vertical band remove (horizontal periodic noise)
        masked[:crow - 10, ccol - 4:ccol + 4] = 1
        masked[crow + 10:, ccol - 4:ccol + 4] = 1
    elif noise_type == 3:
        # main diagonal
        for x in range(H):
            y = x
            if 0 <= y < W:
                for i in range(10):
                    xx = max(0, min(H - 1, x - i))
                    masked[xx, y] = 1
    elif noise_type == 4:
        # anti-diagonal
        for x in range(H):
            y = W - 1 - x
            if 0 <= y < W:
                for i in range(10):
                    xx = max(0, min(H - 1, x - i))
                    masked[xx, y] = 1
    return masked


def _denoise_periodic_fourier(rgb: np.ndarray, noise_type: int, D0: float, order: int) -> np.ndarray:
    # Apply per-channel FFT, mask periodic components, low-pass filter with Butterworth, then inverse FFT
    H, W, C = rgb.shape
    out = np.zeros_like(rgb, dtype=np.float32)
    Hlp = _butterworth_lp(D0, (H, W), order)
    for c in range(C):
        ch = rgb[:, :, c].astype(np.float32)
        # FFT
        F = np.fft.fft2(ch)
        Fshift = np.fft.fftshift(F)
        # Mask periodic spikes
        Fmasked = _apply_mask_for_noise_type(Fshift, noise_type)
        # Optional low-pass filter to soften residual high-frequency noise
        Ffiltered = Fmasked * Hlp
        # Inverse FFT
        ishift = np.fft.ifftshift(Ffiltered)
        rec = np.fft.ifft2(ishift)
        rec = np.real(rec)
        # Normalize to 0..255
        rec = np.clip(rec, 0, 255)
        out[:, :, c] = rec
    return out.astype(np.uint8)


@router.post("/process/denoise-brush")
async def denoise_brush(
    image: UploadFile = File(...),
    mask: UploadFile = File(...),
    strength: float = Form(0.5),
    jpeg_quality: int = Form(90),
):
    """
    Apply denoising only to masked regions using OpenCV fastNlMeansDenoisingColored.
    The mask should be a grayscale/binary image where white (255) indicates areas to denoise.
    """
    try:
        # Read original image
        img_data = await image.read()
        rgb, alpha = _read_image_keep_alpha(img_data)
        
        # Read mask
        mask_data = await mask.read()
        mask_img = Image.open(BytesIO(mask_data)).convert("L")
        mask_arr = np.array(mask_img)
        
        # Resize mask to match image if needed
        if mask_arr.shape[:2] != rgb.shape[:2]:
            mask_arr = cv2.resize(mask_arr, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST)
        
        # Normalize mask to 0-1 range
        mask_normalized = mask_arr.astype(np.float32) / 255.0
        
        # Apply denoising to the entire image
        denoised_rgb = _denoise_cv2(
            rgb,
            float(max(0.0, min(1.0, strength))),
            None,  # h_luma
            None,  # h_color
            7,     # template_size
            21,    # search_size
            False  # use_gray
        )
        
        # Blend: use denoised where mask is white, original where mask is black
        mask_3ch = np.stack([mask_normalized] * 3, axis=-1)
        out_rgb = (rgb.astype(np.float32) * (1 - mask_3ch) + denoised_rgb.astype(np.float32) * mask_3ch)
        out_rgb = np.clip(out_rgb, 0, 255).astype(np.uint8)
        
        out_img = _merge_alpha(out_rgb, alpha)
        
        buf = BytesIO()
        name = image.filename or "image"
        mime = "image/png" if out_img.mode == "RGBA" else "image/jpeg"
        if mime == "image/png":
            out_img.save(buf, format="PNG", optimize=True, compress_level=6)
            fname = os.path.splitext(name)[0] + "_denoised.png"
        else:
            if out_img.mode != "RGB":
                out_img = out_img.convert("RGB")
            out_img.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
            fname = os.path.splitext(name)[0] + "_denoised.jpg"
        buf.seek(0)
        
        return StreamingResponse(BytesIO(buf.getvalue()), media_type=mime, headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        logger.error(f"Brush denoise failed: {e}")
        return JSONResponse({"error": f"Failed to process: {e}"}, status_code=400)


@router.post("/process/denoise-images")
async def denoise_images(
    files: List[UploadFile] = File(...),
    strength: float = Form(0.5),
    jpeg_quality: int = Form(90),
    method: str = Form("opencv"),
    noise_type: int = Form(1),
    fourier_d0: float = Form(80.0),
    fourier_order: int = Form(10),
    nlm_h_luma: Optional[int] = Form(None),
    nlm_h_color: Optional[int] = Form(None),
    nlm_template_size: int = Form(7),
    nlm_search_size: int = Form(21),
    nlm_grayscale: bool = Form(False),
):
    if not files:
        return JSONResponse({"error": "No files provided"}, status_code=400)

    # Prepare models/utilities based on method
    method = (method or "auto").lower().strip()
    model = None
    if method in ("auto", "dncnn"):
        weights = os.getenv("DNCNN_WEIGHTS", "/home/ubuntu/models/dncnn.pth")
        logger.info(f"Attempting to load DnCNN model from: {weights}")
        model = _load_dncnn(weights)

    outputs: List[tuple[str, bytes, str]] = []

    for up in files:
        try:
            name = up.filename or "image"
            ext = os.path.splitext(name)[1].lower() or ".jpg"
            data = await up.read()
            rgb, alpha = _read_image_keep_alpha(data)
            if method == "fourier":
                nt = int(noise_type)
                d0 = float(max(1.0, min(2048.0, fourier_d0)))
                ord_n = int(max(1, min(20, fourier_order)))
                logger.info(f"Using Fourier periodic removal (noise_type={nt}, D0={d0}, n={ord_n}) for {name}")
                out_rgb = _denoise_periodic_fourier(rgb, nt, d0, ord_n)
            elif (method in ("auto", "dncnn")) and (model is not None):
                logger.info(f"Using DnCNN model for denoising {name}")
                out_rgb = _denoise_dncnn(rgb, float(max(0.0, min(1.0, strength))), model)
            else:
                logger.info(
                    f"Using OpenCV FastNLMeans for denoising {name} (h_luma={nlm_h_luma}, h_color={nlm_h_color}, template={nlm_template_size}, search={nlm_search_size}, gray={nlm_grayscale})"
                )
                out_rgb = _denoise_cv2(
                    rgb,
                    float(max(0.0, min(1.0, strength))),
                    nlm_h_luma,
                    nlm_h_color,
                    int(nlm_template_size),
                    int(nlm_search_size),
                    bool(nlm_grayscale),
                )

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
