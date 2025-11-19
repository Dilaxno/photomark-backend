"""Background removal router with Rembg and SAM integration"""
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import StreamingResponse
import json
import io
import base64
import numpy as np
from PIL import Image, ImageDraw
import os
import sys

# Handle different import paths for local vs server
try:
    from core.config import logger
except ImportError:
    from core.config import logger

try:
    from core.auth import resolve_workspace_uid
except ImportError:
    from core.auth import resolve_workspace_uid

try:
    from utils.storage import read_json_key
except ImportError:
    from utils.storage import read_json_key

# transparent-background import (cleaner alternative to rembg, PyTorch-only, no TensorFlow)
try:
    from transparent_background import Remover
    BG_REMOVER_AVAILABLE = True
except ImportError:
    BG_REMOVER_AVAILABLE = False
    logger.warning("transparent-background not available")

# Global state
_sam_predictor = None
_rembg_session = None

# Create router
router = APIRouter(prefix="/api/background-removal", tags=["background-removal"])

# ---------------- HELPERS ----------------
def _get_mobile_sam_predictor():
    """Lazy load SAM predictor (using lightweight SAM model)"""
    global _sam_predictor
    if _sam_predictor is None:
        try:
            from segment_anything import sam_model_registry, SamPredictor
            
            # Try to use lightweight SAM model (vit_b is smaller than vit_h)
            model_type = "vit_b"
            sam_checkpoint = "sam_vit_b_01ec64.pth"
            
            # Download model if not exists
            if not os.path.exists(sam_checkpoint):
                import urllib.request
                logger.info(f"Downloading SAM {model_type} checkpoint...")
                url = "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth"
                try:
                    urllib.request.urlretrieve(url, sam_checkpoint)
                    logger.info("SAM checkpoint downloaded successfully")
                except Exception as download_err:
                    logger.error(f"Failed to download SAM checkpoint: {download_err}")
                    logger.warning("SAM features will not be available. Using Rembg only.")
                    return None
            
            sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
            sam.eval()
            _sam_predictor = SamPredictor(sam)
            logger.info("SAM predictor loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load SAM: {e}")
            logger.warning("SAM features will not be available. Using Rembg only.")
            return None
    return _sam_predictor

def _get_bg_remover():
    """Get or create background remover instance"""
    global _rembg_session
    if _rembg_session is None:
        try:
            # Initialize remover with fast mode for better performance
            # Expand ~ to home directory properly
            model_dir = os.path.expanduser('~/.transparent-background/models')
            os.makedirs(model_dir, exist_ok=True)
            
            # Auto-detect device (cuda if available, else cpu)
            device = 'cuda:0' if os.system('nvidia-smi > /dev/null 2>&1') == 0 else 'cpu'
            
            _rembg_session = Remover(mode='fast', jit=True, device=device, ckpt=model_dir)
            logger.info(f"Background remover loaded successfully on {device}")
        except Exception as e:
            logger.error(f"Failed to load background remover: {e}")
            _rembg_session = None
    return _rembg_session

def _billing_uid_from_request(request: Request) -> str:
    eff_uid, _ = resolve_workspace_uid(request)
    if eff_uid:
        return eff_uid
    try:
        ip = request.client.host if getattr(request, 'client', None) else 'unknown'
    except Exception:
        ip = 'unknown'
    return f"anon:{ip}"

def _is_paid_customer(uid: str) -> bool:
    try:
        ent = read_json_key(f"users/{uid}/billing/entitlement.json") or {}
        plan = str(ent.get('plan') or '').strip().lower()
        if plan and plan != 'free':
            return True
        return bool(ent.get('isPaid'))
    except Exception:
        return False

def _image_to_base64(img: Image.Image, format: str = "PNG") -> str:
    """Convert PIL Image to base64 string"""
    buffer = io.BytesIO()
    img.save(buffer, format=format)
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode('utf-8')

def _base64_to_image(base64_str: str) -> Image.Image:
    """Convert base64 string to PIL Image"""
    img_data = base64.b64decode(base64_str)
    return Image.open(io.BytesIO(img_data))

# ---------------- ENDPOINTS ----------------

@router.post("/step1-rembg")
async def step1_rembg_cutout(
    request: Request,
    image: UploadFile = File(...)
):
    """
    Step 1: Use Rembg (U2-Net) for initial automatic background removal
    Returns: Original image, mask, and preview with transparent background
    """
    try:
        # Read image
        img_bytes = await image.read()
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        
        # Apply background removal
        remover = _get_bg_remover()
        if remover is None:
            raise HTTPException(status_code=503, detail="Background removal model not available")
        
        # Process image (returns RGBA with transparent background)
        output = remover.process(img, type='rgba')
        
        # Extract mask from alpha channel
        if output.mode == "RGBA":
            mask = np.array(output.split()[-1])
        else:
            # Fallback if no alpha
            mask = np.ones((output.height, output.width), dtype=np.uint8) * 255
        
        # Create mask image
        mask_img = Image.fromarray(mask, mode="L")
        
        # Create preview (image with transparent background)
        preview = output.convert("RGBA")
        
        # Convert to base64 for JSON response
        original_b64 = _image_to_base64(img, "JPEG")
        mask_b64 = _image_to_base64(mask_img, "PNG")
        preview_b64 = _image_to_base64(preview, "PNG")
        
        return {
            "success": True,
            "original": original_b64,
            "mask": mask_b64,
            "preview": preview_b64,
            "width": img.width,
            "height": img.height
        }
    
    except Exception as e:
        logger.error(f"Rembg cutout failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/step2-mobile-sam")
async def step2_mobile_sam_mask(
    request: Request,
    image_base64: str = Form(...),
    click_points: str = Form(...)  # JSON array of {x, y, type: "positive"|"negative"}
):
    """
    Step 2: Use Mobile-SAM for precise mask extraction based on user clicks
    User clicks on object → model extracts precise mask → display overlay
    """
    try:
        # Parse click points
        points = json.loads(click_points)
        if not points:
            raise HTTPException(status_code=400, detail="No click points provided")
        
        # Convert base64 to image
        img = _base64_to_image(image_base64)
        img_np = np.array(img.convert("RGB"))
        
        # Get Mobile-SAM predictor
        predictor = _get_mobile_sam_predictor()
        if predictor is None:
            raise HTTPException(status_code=503, detail="Mobile-SAM model not available")
        
        # Set image for predictor
        predictor.set_image(img_np)
        
        # Prepare points and labels
        point_coords = []
        point_labels = []
        for pt in points:
            point_coords.append([pt['x'], pt['y']])
            point_labels.append(1 if pt['type'] == 'positive' else 0)
        
        point_coords = np.array(point_coords)
        point_labels = np.array(point_labels)
        
        # Predict mask
        masks, scores, logits = predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            multimask_output=True
        )
        
        # Use best mask (highest score)
        best_mask_idx = np.argmax(scores)
        mask = masks[best_mask_idx]
        
        # Convert mask to uint8
        mask_uint8 = (mask * 255).astype(np.uint8)
        mask_img = Image.fromarray(mask_uint8, mode="L")
        
        # Create preview with mask overlay
        preview = img.convert("RGBA")
        mask_overlay = Image.new("RGBA", preview.size, (0, 255, 0, 100))
        mask_overlay.putalpha(Image.fromarray((mask * 100).astype(np.uint8)))
        preview = Image.alpha_composite(preview, mask_overlay)
        
        # Convert to base64
        mask_b64 = _image_to_base64(mask_img, "PNG")
        preview_b64 = _image_to_base64(preview, "PNG")
        
        return {
            "success": True,
            "mask": mask_b64,
            "preview": preview_b64,
            "score": float(scores[best_mask_idx])
        }
    
    except Exception as e:
        logger.error(f"Mobile-SAM mask extraction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/step3-refine-brush")
async def step3_refine_brush_mask(
    request: Request,
    mask_base64: str = Form(...),
    brush_strokes: str = Form(...)  # JSON array of {points: [{x, y}], mode: "add"|"remove", size: number}
):
    """
    Step 3: Refine mask using brush strokes (add or remove areas)
    """
    try:
        # Parse brush strokes
        strokes = json.loads(brush_strokes)
        
        # Convert base64 mask to image
        mask = _base64_to_image(mask_base64).convert("L")
        mask_np = np.array(mask)
        
        # Apply brush strokes
        for stroke in strokes:
            points = stroke['points']
            mode = stroke['mode']
            size = stroke.get('size', 20)
            
            if len(points) < 2:
                continue
            
            # Create a drawing context
            mask_pil = Image.fromarray(mask_np, mode="L")
            draw = ImageDraw.Draw(mask_pil)
            
            # Draw lines between points
            for i in range(len(points) - 1):
                x1, y1 = points[i]['x'], points[i]['y']
                x2, y2 = points[i + 1]['x'], points[i + 1]['y']
                
                # Draw thick line using circles
                color = 255 if mode == "add" else 0
                draw.line([(x1, y1), (x2, y2)], fill=color, width=size)
                draw.ellipse([x1 - size // 2, y1 - size // 2, x1 + size // 2, y1 + size // 2], fill=color)
            
            # Draw final point
            if points:
                x, y = points[-1]['x'], points[-1]['y']
                color = 255 if mode == "add" else 0
                draw.ellipse([x - size // 2, y - size // 2, x + size // 2, y + size // 2], fill=color)
            
            mask_np = np.array(mask_pil)
        
        # Convert refined mask to base64
        refined_mask = Image.fromarray(mask_np, mode="L")
        mask_b64 = _image_to_base64(refined_mask, "PNG")
        
        return {
            "success": True,
            "mask": mask_b64
        }
    
    except Exception as e:
        logger.error(f"Brush refinement failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/step4-generate-cutout")
async def step4_generate_final_cutout(
    request: Request,
    image_base64: str = Form(...),
    mask_base64: str = Form(...),
    output_format: str = Form("png")  # png or jpg
):
    """
    Step 4: Generate final cutout with transparent or white background using Pillow
    """
    try:
        # Convert base64 to images
        img = _base64_to_image(image_base64).convert("RGB")
        mask = _base64_to_image(mask_base64).convert("L")
        
        # Ensure same size
        if img.size != mask.size:
            mask = mask.resize(img.size, Image.LANCZOS)
        
        # Create cutout
        if output_format.lower() == "png":
            # Transparent background
            cutout = Image.new("RGBA", img.size, (0, 0, 0, 0))
            img_rgba = img.convert("RGBA")
            cutout.paste(img_rgba, (0, 0), mask)
            format_str = "PNG"
        else:
            # White background for JPG
            cutout = Image.new("RGB", img.size, (255, 255, 255))
            cutout.paste(img, (0, 0), mask)
            format_str = "JPEG"
        
        # Convert to bytes for download
        buffer = io.BytesIO()
        cutout.save(buffer, format=format_str, quality=95)
        buffer.seek(0)
        
        return StreamingResponse(
            buffer,
            media_type=f"image/{output_format.lower()}",
            headers={
                "Content-Disposition": f"attachment; filename=cutout.{output_format.lower()}"
            }
        )
    
    except Exception as e:
        logger.error(f"Final cutout generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate-preview")
async def generate_preview(
    request: Request,
    image_base64: str = Form(...),
    mask_base64: str = Form(...)
):
    """
    Generate preview of cutout with checkerboard background
    """
    try:
        # Convert base64 to images
        img = _base64_to_image(image_base64).convert("RGB")
        mask = _base64_to_image(mask_base64).convert("L")
        
        # Ensure same size
        if img.size != mask.size:
            mask = mask.resize(img.size, Image.LANCZOS)
        
        # Create checkerboard background
        checker_size = 20
        checker = Image.new("RGB", img.size, (255, 255, 255))
        draw = ImageDraw.Draw(checker)
        for y in range(0, img.height, checker_size):
            for x in range(0, img.width, checker_size):
                if (x // checker_size + y // checker_size) % 2 == 0:
                    draw.rectangle([x, y, x + checker_size, y + checker_size], fill=(200, 200, 200))
        
        # Composite image over checkerboard
        img_rgba = img.convert("RGBA")
        cutout = Image.new("RGBA", img.size, (0, 0, 0, 0))
        cutout.paste(img_rgba, (0, 0), mask)
        
        checker_rgba = checker.convert("RGBA")
        preview = Image.alpha_composite(checker_rgba, cutout)
        
        # Convert to base64
        preview_b64 = _image_to_base64(preview, "PNG")
        
        return {
            "success": True,
            "preview": preview_b64
        }
    
    except Exception as e:
        logger.error(f"Preview generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
