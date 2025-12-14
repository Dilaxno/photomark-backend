from __future__ import annotations

import os
import logging
from io import BytesIO
from typing import List, Optional

import numpy as np
import cv2
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image

logger = logging.getLogger(__name__)

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
  # Apply gamma-based exposure lift based on strength (higher strength -> brighter)
  s = float(max(0.0, min(1.0, strength)))
  enh_f = np.array(out_img, dtype=np.float32) / 255.0
  gamma = max(0.35, 1.0 - 0.6 * s)
  enh_f = np.clip(np.power(enh_f, gamma), 0.0, 1.0)
  enh = np.clip(enh_f * 255.0 + 0.5, 0, 255).astype(np.uint8)
  if 0.0 <= s <= 1.0:
    base = np.array(img.convert("RGB"), dtype=np.float32)
    mix = (1.0 - s) * base + s * enh.astype(np.float32)
    mix = np.clip(mix + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(mix, mode="RGB")
  return out_img

def _enhance_fallback(img: Image.Image, strength: float) -> Image.Image:
  import cv2
  rgb = np.array(img.convert("RGB"))
  lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
  l, a, b = cv2.split(lab)
  s = float(max(0.0, min(1.0, strength)))
  clahe = cv2.createCLAHE(clipLimit=2.0 + s * 6.0, tileGridSize=(8,8))
  l2 = clahe.apply(l)
  lab2 = cv2.merge((l2, a, b))
  rgb2 = cv2.cvtColor(lab2, cv2.COLOR_LAB2RGB)
  # Apply additional brightness/contrast based on strength
  alpha = 1.0 + 0.45 * s  # contrast gain
  beta = int(18 * s)      # brightness offset
  adj = cv2.convertScaleAbs(rgb2, alpha=alpha, beta=beta)
  # Blend with original according to strength to respect user intent
  mix = (1.0 - s) * rgb.astype(np.float32) + s * adj.astype(np.float32)
  mix = np.clip(mix + 0.5, 0, 255).astype(np.uint8)
  return Image.fromarray(mix, mode="RGB")

# ============== Intrinsic Image Decomposition for Manual Relighting ==============

def _decompose_intrinsic(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Decompose image into albedo (reflectance) and shading components.
    Uses a simplified Retinex-based approach suitable for real-time editing.
    
    Returns:
        albedo: Color/texture information (HxWx3, float32, 0-1)
        shading: Illumination/lighting map (HxW, float32, 0-1)
    """
    # Convert to float
    img_f = rgb.astype(np.float32) / 255.0
    
    # Convert to LAB for better luminance separation
    lab = cv2.cvtColor((img_f * 255).astype(np.uint8), cv2.COLOR_RGB2LAB)
    L = lab[:, :, 0].astype(np.float32) / 255.0
    
    # Estimate shading using bilateral filter (edge-preserving smoothing)
    # This captures large-scale illumination while preserving edges
    L_smooth = cv2.bilateralFilter(L, d=9, sigmaColor=0.1, sigmaSpace=75)
    
    # Apply guided filter for better edge preservation
    try:
        L_smooth = cv2.ximgproc.guidedFilter(L, L_smooth, radius=16, eps=0.01)
    except:
        # Fallback if ximgproc not available
        L_smooth = cv2.GaussianBlur(L_smooth, (31, 31), 0)
    
    # Shading is the smoothed luminance
    shading = np.clip(L_smooth, 0.01, 1.0)
    
    # Albedo = Original / Shading (Retinex decomposition)
    # Expand shading to 3 channels for division
    shading_3ch = np.stack([shading] * 3, axis=-1)
    albedo = np.clip(img_f / (shading_3ch + 1e-6), 0, 1)
    
    return albedo, shading


def _recompose_image(albedo: np.ndarray, shading: np.ndarray) -> np.ndarray:
    """
    Recompose image from albedo and shading.
    
    Args:
        albedo: Color/texture (HxWx3, float32, 0-1)
        shading: Illumination map (HxW, float32, 0-1)
    
    Returns:
        RGB image (HxWx3, uint8, 0-255)
    """
    shading_3ch = np.stack([shading] * 3, axis=-1)
    result = albedo * shading_3ch
    result = np.clip(result * 255, 0, 255).astype(np.uint8)
    return result


def _adjust_shading(
    shading: np.ndarray,
    intensity: float = 1.0,
    shadow_strength: float = 1.0,
    highlight_strength: float = 1.0,
    gamma: float = 1.0
) -> np.ndarray:
    """
    Adjust the shading map with various controls.
    
    Args:
        shading: Original shading map (HxW, float32, 0-1)
        intensity: Overall brightness multiplier (0.5-2.0)
        shadow_strength: Darken/lighten shadows (0-2, 1=neutral)
        highlight_strength: Darken/lighten highlights (0-2, 1=neutral)
        gamma: Gamma correction for midtones
    
    Returns:
        Adjusted shading map
    """
    result = shading.copy()
    
    # Apply gamma correction
    if gamma != 1.0:
        result = np.power(result, 1.0 / gamma)
    
    # Adjust shadows (dark areas)
    if shadow_strength != 1.0:
        shadow_mask = 1.0 - result  # Inverse: high where dark
        shadow_adjustment = shadow_mask * (shadow_strength - 1.0) * 0.3
        result = result + shadow_adjustment
    
    # Adjust highlights (bright areas)
    if highlight_strength != 1.0:
        highlight_mask = result  # High where bright
        highlight_adjustment = highlight_mask * (highlight_strength - 1.0) * 0.3
        result = result + highlight_adjustment
    
    # Apply overall intensity
    result = result * intensity
    
    return np.clip(result, 0.01, 1.5)


def _apply_light_brush(
    shading: np.ndarray,
    mask: np.ndarray,
    light_intensity: float = 1.5,
    feather: int = 15
) -> np.ndarray:
    """
    Apply painted light to shading map based on brush mask.
    
    Args:
        shading: Original shading map (HxW, float32, 0-1)
        mask: Brush mask (HxW, uint8, 0-255) where white = add light
        light_intensity: How much to brighten painted areas (0.5-3.0)
        feather: Blur radius for soft edges
    
    Returns:
        Modified shading map
    """
    # Normalize mask
    mask_f = mask.astype(np.float32) / 255.0
    
    # Feather the mask for soft edges
    if feather > 0:
        mask_f = cv2.GaussianBlur(mask_f, (feather * 2 + 1, feather * 2 + 1), 0)
    
    # Calculate light addition
    light_add = mask_f * (light_intensity - 1.0)
    
    # Apply to shading
    result = shading + light_add * shading
    
    return np.clip(result, 0.01, 2.0)


def _apply_shadow_brush(
    shading: np.ndarray,
    mask: np.ndarray,
    shadow_intensity: float = 0.5,
    feather: int = 15
) -> np.ndarray:
    """
    Apply painted shadows to shading map based on brush mask.
    
    Args:
        shading: Original shading map (HxW, float32, 0-1)
        mask: Brush mask (HxW, uint8, 0-255) where white = add shadow
        shadow_intensity: How much to darken painted areas (0-1, lower = darker)
        feather: Blur radius for soft edges
    
    Returns:
        Modified shading map
    """
    # Normalize mask
    mask_f = mask.astype(np.float32) / 255.0
    
    # Feather the mask for soft edges
    if feather > 0:
        mask_f = cv2.GaussianBlur(mask_f, (feather * 2 + 1, feather * 2 + 1), 0)
    
    # Blend between original and darkened
    darkened = shading * shadow_intensity
    result = shading * (1 - mask_f) + darkened * mask_f
    
    return np.clip(result, 0.01, 1.5)


@router.post("/process/relight-decompose")
async def relight_decompose(
    image: UploadFile = File(...),
):
    """
    Decompose an image into albedo and shading maps for manual editing.
    Returns a JSON with base64-encoded albedo and shading images.
    """
    try:
        data = await image.read()
        img = Image.open(BytesIO(data)).convert("RGB")
        rgb = np.array(img)
        
        albedo, shading = _decompose_intrinsic(rgb)
        
        # Convert albedo to image
        albedo_img = Image.fromarray((albedo * 255).astype(np.uint8), mode="RGB")
        albedo_buf = BytesIO()
        albedo_img.save(albedo_buf, format="PNG")
        albedo_buf.seek(0)
        
        # Convert shading to grayscale image
        shading_img = Image.fromarray((shading * 255).astype(np.uint8), mode="L")
        shading_buf = BytesIO()
        shading_img.save(shading_buf, format="PNG")
        shading_buf.seek(0)
        
        import base64
        albedo_b64 = base64.b64encode(albedo_buf.getvalue()).decode('utf-8')
        shading_b64 = base64.b64encode(shading_buf.getvalue()).decode('utf-8')
        
        return JSONResponse({
            "albedo": f"data:image/png;base64,{albedo_b64}",
            "shading": f"data:image/png;base64,{shading_b64}",
            "width": rgb.shape[1],
            "height": rgb.shape[0]
        })
    except Exception as e:
        logger.error(f"Decompose failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/process/relight-manual")
async def relight_manual(
    image: UploadFile = File(...),
    light_mask: Optional[UploadFile] = File(None),
    shadow_mask: Optional[UploadFile] = File(None),
    intensity: float = Form(1.0),
    shadow_strength: float = Form(1.0),
    highlight_strength: float = Form(1.0),
    gamma: float = Form(1.0),
    light_intensity: float = Form(1.5),
    shadow_intensity: float = Form(0.5),
    feather: int = Form(15),
    jpeg_quality: int = Form(92),
):
    """
    Apply manual relighting with brush-painted light and shadow masks.
    
    Args:
        image: Original image
        light_mask: Grayscale mask where white = add light
        shadow_mask: Grayscale mask where white = add shadow
        intensity: Overall shading intensity (0.5-2.0)
        shadow_strength: Shadow adjustment (0-2)
        highlight_strength: Highlight adjustment (0-2)
        gamma: Gamma correction (0.5-2.0)
        light_intensity: Brightness of painted lights (0.5-3.0)
        shadow_intensity: Darkness of painted shadows (0-1)
        feather: Brush feather radius
    """
    try:
        # Read original image
        img_data = await image.read()
        img = Image.open(BytesIO(img_data)).convert("RGB")
        rgb = np.array(img)
        
        # Decompose into albedo and shading
        albedo, shading = _decompose_intrinsic(rgb)
        
        # Apply global shading adjustments
        shading = _adjust_shading(
            shading,
            intensity=float(max(0.5, min(2.0, intensity))),
            shadow_strength=float(max(0.0, min(2.0, shadow_strength))),
            highlight_strength=float(max(0.0, min(2.0, highlight_strength))),
            gamma=float(max(0.5, min(2.0, gamma)))
        )
        
        # Apply light brush if provided
        if light_mask is not None:
            mask_data = await light_mask.read()
            mask_img = Image.open(BytesIO(mask_data)).convert("L")
            mask_arr = np.array(mask_img)
            
            # Resize mask if needed
            if mask_arr.shape[:2] != shading.shape[:2]:
                mask_arr = cv2.resize(mask_arr, (shading.shape[1], shading.shape[0]), interpolation=cv2.INTER_LINEAR)
            
            shading = _apply_light_brush(
                shading, mask_arr,
                light_intensity=float(max(0.5, min(3.0, light_intensity))),
                feather=int(max(0, min(50, feather)))
            )
        
        # Apply shadow brush if provided
        if shadow_mask is not None:
            mask_data = await shadow_mask.read()
            mask_img = Image.open(BytesIO(mask_data)).convert("L")
            mask_arr = np.array(mask_img)
            
            # Resize mask if needed
            if mask_arr.shape[:2] != shading.shape[:2]:
                mask_arr = cv2.resize(mask_arr, (shading.shape[1], shading.shape[0]), interpolation=cv2.INTER_LINEAR)
            
            shading = _apply_shadow_brush(
                shading, mask_arr,
                shadow_intensity=float(max(0.0, min(1.0, shadow_intensity))),
                feather=int(max(0, min(50, feather)))
            )
        
        # Recompose the image
        result = _recompose_image(albedo, shading)
        
        # Save result
        out_img = Image.fromarray(result, mode="RGB")
        buf = BytesIO()
        out_img.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
        buf.seek(0)
        
        name = image.filename or "image"
        fname = os.path.splitext(name)[0] + "_relit.jpg"
        
        return StreamingResponse(BytesIO(buf.getvalue()), media_type="image/jpeg", headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        logger.error(f"Manual relight failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


def _compute_normal_map(rgb: np.ndarray) -> np.ndarray:
    """
    Estimate surface normals from image using gradient-based approach.
    This is a simplified normal estimation suitable for relighting.
    
    Returns:
        Normal map (HxWx3, float32, normalized vectors)
    """
    # Convert to grayscale for gradient computation
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    
    # Compute gradients using Sobel operators
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    
    # Scale gradients for better normal estimation
    scale = 2.0
    grad_x = grad_x * scale
    grad_y = grad_y * scale
    
    # Construct normal vectors: N = (-dI/dx, -dI/dy, 1) normalized
    normals = np.zeros((rgb.shape[0], rgb.shape[1], 3), dtype=np.float32)
    normals[:, :, 0] = -grad_x
    normals[:, :, 1] = -grad_y
    normals[:, :, 2] = 1.0
    
    # Normalize
    norm = np.sqrt(np.sum(normals ** 2, axis=2, keepdims=True))
    normals = normals / (norm + 1e-8)
    
    return normals


def _apply_directional_light(
    rgb: np.ndarray,
    light_x: float,
    light_y: float,
    light_intensity: float = 1.5,
    ambient: float = 0.3,
    diffuse_strength: float = 0.7,
    specular_strength: float = 0.2,
    specular_power: float = 16.0,
    light_color: tuple = (255, 255, 255),
    falloff: float = 0.5
) -> np.ndarray:
    """
    Apply directional lighting to an image based on light source position.
    Uses Phong-like shading model with estimated normals.
    
    Args:
        rgb: Input image (HxWx3, uint8)
        light_x: Light X position (0-1, relative to image width)
        light_y: Light Y position (0-1, relative to image height)
        light_intensity: Overall light brightness multiplier
        ambient: Ambient light level (0-1)
        diffuse_strength: Diffuse lighting strength
        specular_strength: Specular highlight strength
        specular_power: Specular shininess
        light_color: RGB color of the light
        falloff: Distance falloff factor (0=no falloff, 1=strong falloff)
    
    Returns:
        Relit image (HxWx3, uint8)
    """
    h, w = rgb.shape[:2]
    
    # Estimate normals from the image
    normals = _compute_normal_map(rgb)
    
    # Create coordinate grids
    y_coords, x_coords = np.mgrid[0:h, 0:w].astype(np.float32)
    x_coords = x_coords / w
    y_coords = y_coords / h
    
    # Light position in normalized coordinates
    # Light is positioned above the image plane (z > 0)
    light_z = 0.5  # Height of light above image
    
    # Compute light direction vectors for each pixel
    light_dir = np.zeros((h, w, 3), dtype=np.float32)
    light_dir[:, :, 0] = light_x - x_coords
    light_dir[:, :, 1] = light_y - y_coords
    light_dir[:, :, 2] = light_z
    
    # Compute distance for falloff
    dist = np.sqrt(np.sum(light_dir[:, :, :2] ** 2, axis=2))
    
    # Normalize light direction
    light_norm = np.sqrt(np.sum(light_dir ** 2, axis=2, keepdims=True))
    light_dir = light_dir / (light_norm + 1e-8)
    
    # Compute diffuse lighting (N dot L)
    n_dot_l = np.sum(normals * light_dir, axis=2)
    n_dot_l = np.clip(n_dot_l, 0, 1)
    
    # Compute view direction (assuming viewer is directly above)
    view_dir = np.zeros((h, w, 3), dtype=np.float32)
    view_dir[:, :, 2] = 1.0
    
    # Compute reflection direction for specular
    reflect_dir = 2 * n_dot_l[:, :, np.newaxis] * normals - light_dir
    
    # Specular component (R dot V)^n
    r_dot_v = np.sum(reflect_dir * view_dir, axis=2)
    r_dot_v = np.clip(r_dot_v, 0, 1)
    specular = np.power(r_dot_v, specular_power)
    
    # Apply distance falloff
    attenuation = 1.0 / (1.0 + falloff * dist * dist * 4)
    
    # Combine lighting components
    diffuse = n_dot_l * diffuse_strength * attenuation
    specular = specular * specular_strength * attenuation
    
    # Total lighting
    lighting = ambient + (diffuse + specular) * light_intensity
    lighting = np.clip(lighting, 0, 2.0)
    
    # Apply lighting to image
    img_f = rgb.astype(np.float32)
    
    # Apply light color
    light_color_f = np.array(light_color, dtype=np.float32) / 255.0
    
    # Modulate by lighting
    result = img_f * lighting[:, :, np.newaxis]
    
    # Add specular highlights with light color
    specular_contrib = specular[:, :, np.newaxis] * light_color_f * 255.0 * light_intensity
    result = result + specular_contrib
    
    result = np.clip(result, 0, 255).astype(np.uint8)
    
    return result


def _apply_directional_light_advanced(
    rgb: np.ndarray,
    light_x: float,
    light_y: float,
    light_intensity: float = 1.5,
    ambient: float = 0.3,
    shadow_softness: float = 0.5,
    light_color: tuple = (255, 255, 255),
    preserve_colors: bool = True
) -> np.ndarray:
    """
    Advanced directional relighting using intrinsic decomposition.
    Preserves original colors better while changing lighting direction.
    
    Args:
        rgb: Input image (HxWx3, uint8)
        light_x: Light X position (0-1)
        light_y: Light Y position (0-1)
        light_intensity: Light brightness
        ambient: Ambient light level
        shadow_softness: How soft shadows should be
        light_color: RGB color of light
        preserve_colors: Whether to preserve original colors
    
    Returns:
        Relit image (HxWx3, uint8)
    """
    h, w = rgb.shape[:2]
    
    # Decompose into albedo and shading
    albedo, original_shading = _decompose_intrinsic(rgb)
    
    # Estimate normals
    normals = _compute_normal_map(rgb)
    
    # Create coordinate grids
    y_coords, x_coords = np.mgrid[0:h, 0:w].astype(np.float32)
    x_coords = x_coords / w
    y_coords = y_coords / h
    
    # Light direction
    light_z = 0.6
    light_dir = np.zeros((h, w, 3), dtype=np.float32)
    light_dir[:, :, 0] = light_x - x_coords
    light_dir[:, :, 1] = light_y - y_coords
    light_dir[:, :, 2] = light_z
    
    # Distance for falloff
    dist = np.sqrt(light_dir[:, :, 0]**2 + light_dir[:, :, 1]**2)
    
    # Normalize
    light_norm = np.sqrt(np.sum(light_dir ** 2, axis=2, keepdims=True))
    light_dir = light_dir / (light_norm + 1e-8)
    
    # Diffuse lighting
    n_dot_l = np.sum(normals * light_dir, axis=2)
    n_dot_l = np.clip(n_dot_l, 0, 1)
    
    # Soft shadows using distance
    shadow_factor = 1.0 / (1.0 + shadow_softness * dist * dist * 2)
    
    # New shading map
    new_shading = ambient + n_dot_l * light_intensity * shadow_factor
    
    # Blend with original shading to preserve some original lighting
    blend_factor = 0.6
    final_shading = blend_factor * new_shading + (1 - blend_factor) * original_shading
    final_shading = np.clip(final_shading, 0.05, 2.0)
    
    # Recompose
    if preserve_colors:
        # Use original albedo
        result = _recompose_image(albedo, final_shading)
    else:
        # Apply light color tint
        light_color_f = np.array(light_color, dtype=np.float32) / 255.0
        tinted_albedo = albedo * light_color_f
        result = _recompose_image(tinted_albedo, final_shading)
    
    return result


@router.post("/process/relight-directional")
async def relight_directional(
    image: UploadFile = File(...),
    light_x: float = Form(0.5),
    light_y: float = Form(0.0),
    light_intensity: float = Form(1.5),
    ambient: float = Form(0.3),
    shadow_softness: float = Form(0.5),
    light_color_r: int = Form(255),
    light_color_g: int = Form(255),
    light_color_b: int = Form(255),
    preserve_colors: bool = Form(True),
    jpeg_quality: int = Form(92),
):
    """
    Apply directional relighting with controllable light source position.
    
    Args:
        image: Input image
        light_x: Light X position (0=left, 0.5=center, 1=right)
        light_y: Light Y position (0=top, 0.5=center, 1=bottom)
        light_intensity: Light brightness (0.5-3.0)
        ambient: Ambient light level (0-1)
        shadow_softness: Shadow softness (0-1)
        light_color_r/g/b: Light color RGB components
        preserve_colors: Whether to preserve original colors
    """
    try:
        data = await image.read()
        img = Image.open(BytesIO(data)).convert("RGB")
        rgb = np.array(img)
        
        # Clamp parameters
        light_x = float(max(0.0, min(1.0, light_x)))
        light_y = float(max(0.0, min(1.0, light_y)))
        light_intensity = float(max(0.5, min(3.0, light_intensity)))
        ambient = float(max(0.0, min(1.0, ambient)))
        shadow_softness = float(max(0.0, min(1.0, shadow_softness)))
        light_color = (
            int(max(0, min(255, light_color_r))),
            int(max(0, min(255, light_color_g))),
            int(max(0, min(255, light_color_b)))
        )
        
        # Apply directional relighting
        result = _apply_directional_light_advanced(
            rgb,
            light_x=light_x,
            light_y=light_y,
            light_intensity=light_intensity,
            ambient=ambient,
            shadow_softness=shadow_softness,
            light_color=light_color,
            preserve_colors=preserve_colors
        )
        
        # Save result
        out_img = Image.fromarray(result, mode="RGB")
        buf = BytesIO()
        out_img.save(buf, format="JPEG", quality=int(max(1, min(95, jpeg_quality))), optimize=True, progressive=True)
        buf.seek(0)
        
        name = image.filename or "image"
        fname = os.path.splitext(name)[0] + "_relit.jpg"
        
        return StreamingResponse(BytesIO(buf.getvalue()), media_type="image/jpeg", headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        logger.error(f"Directional relight failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)


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
