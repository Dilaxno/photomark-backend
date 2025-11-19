import io
import math
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import numpy as np
from PIL import Image

try:
    import mediapipe as mp  # type: ignore
    _MP_AVAILABLE = True
except Exception:
    mp = None  # type: ignore
    _MP_AVAILABLE = False


@dataclass
class BBox:
    x0: int
    y0: int
    x1: int
    y1: int

    def width(self) -> int:
        return max(0, self.x1 - self.x0)

    def height(self) -> int:
        return max(0, self.y1 - self.y0)

    def clamp(self, w: int, h: int) -> "BBox":
        x0 = max(0, min(self.x0, w - 1))
        y0 = max(0, min(self.y0, h - 1))
        x1 = max(0, min(self.x1, w))
        y1 = max(0, min(self.y1, h))
        if x1 <= x0:
            x1 = min(w, x0 + 1)
        if y1 <= y0:
            y1 = min(h, y0 + 1)
        return BBox(x0, y0, x1, y1)

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def area(self) -> int:
        return self.width() * self.height()

    def center(self) -> Tuple[float, float]:
        return (self.x0 + self.width() / 2.0, self.y0 + self.height() / 2.0)


class SmartCropper:
    """
    Smart subject-aware cropper using MediaPipe signals where available.
    Falls back gracefully to center crop if MediaPipe is not available or fails.
    """

    def __init__(self):
        self.mp_face = None
        self.mp_selfie = None
        self.mp_pose = None
        if _MP_AVAILABLE:
            try:
                self.mp_face = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5)
            except Exception:
                self.mp_face = None
            try:
                self.mp_selfie = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=1)
            except Exception:
                self.mp_selfie = None
            try:
                self.mp_pose = mp.solutions.pose.Pose(static_image_mode=True, model_complexity=1, enable_segmentation=False)
            except Exception:
                self.mp_pose = None

    def _np_from_pil(self, img: Image.Image) -> np.ndarray:
        arr = np.array(img.convert("RGB"))
        return arr

    def _bbox_from_rel(self, rel_bbox, w: int, h: int) -> BBox:
        x0 = int(rel_bbox.xmin * w)
        y0 = int(rel_bbox.ymin * h)
        x1 = int((rel_bbox.xmin + rel_bbox.width) * w)
        y1 = int((rel_bbox.ymin + rel_bbox.height) * h)
        return BBox(x0, y0, x1, y1).clamp(w, h)

    def _detect_face_bbox(self, img: Image.Image) -> Optional[BBox]:
        if not self.mp_face:
            return None
        arr = self._np_from_pil(img)
        res = self.mp_face.process(arr)
        if not res.detections:
            return None
        # Choose the highest score detection
        best = max(res.detections, key=lambda d: d.score[0] if d.score else 0.0)
        if not best.location_data or not best.location_data.relative_bounding_box:
            return None
        h, w = arr.shape[:2]
        return self._bbox_from_rel(best.location_data.relative_bounding_box, w, h)

    def _detect_selfie_bbox(self, img: Image.Image) -> Optional[BBox]:
        if not self.mp_selfie:
            return None
        arr = self._np_from_pil(img)
        res = self.mp_selfie.process(arr)
        mask = getattr(res, "segmentation_mask", None)
        if mask is None:
            return None
        m = (mask > 0.5)
        if not m.any():
            return None
        ys, xs = np.where(m)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        h, w = arr.shape[:2]
        return BBox(x0, y0, x1, y1).clamp(w, h)

    def _detect_pose_bbox(self, img: Image.Image) -> Optional[BBox]:
        if not self.mp_pose:
            return None
        arr = self._np_from_pil(img)
        res = self.mp_pose.process(arr)
        lm = getattr(res, "pose_landmarks", None)
        if not lm or not lm.landmark:
            return None
        xs = [p.x for p in lm.landmark if 0 <= p.x <= 1]
        ys = [p.y for p in lm.landmark if 0 <= p.y <= 1]
        if not xs or not ys:
            return None
        h, w = arr.shape[:2]
        x0, x1 = int(min(xs) * w), int(max(xs) * w)
        y0, y1 = int(min(ys) * h), int(max(ys) * h)
        return BBox(x0, y0, x1, y1).clamp(w, h)

    def detect_subject_bbox(self, img: Image.Image) -> Optional[BBox]:
        """Return a conservative bbox around detected subject by unioning available boxes,
        with preference to faces if present.
        """
        b_face = self._detect_face_bbox(img)
        b_self = self._detect_selfie_bbox(img)
        b_pose = self._detect_pose_bbox(img)

        boxes = [b for b in [b_face, b_self, b_pose] if b is not None]
        if not boxes:
            return None
        # If face exists, start from face and expand with union of others
        b = boxes[0]
        if b_face:
            b = b_face
            for bb in [b_self, b_pose]:
                if bb:
                    b = b.union(bb)
        else:
            for bb in boxes[1:]:
                b = b.union(bb)
        return b

    def _expand_to_aspect_around_center(self, center: Tuple[float, float], aspect_w: int, aspect_h: int, img_w: int, img_h: int, margin_scale: float = 1.2) -> BBox:
        """Build a bbox with target aspect ratio centered on the given point, maximizing area within the image,
        and including a margin scale relative to subject bbox size if known.
        """
        target_ar = aspect_w / aspect_h
        # Determine max width/height we can use while keeping center
        cx, cy = center
        # Max distances to edges
        left = cx
        right = img_w - cx
        up = cy
        down = img_h - cy
        # We will fit a box within these bounds
        max_w = 2 * min(left, right)
        max_h = 2 * min(up, down)
        # Adjust by margin scale (smaller to include some background)
        max_w = max_w / margin_scale
        max_h = max_h / margin_scale
        if max_w <= 0 or max_h <= 0:
            return BBox(0, 0, img_w, img_h)
        # Fit aspect
        if max_w / max_h > target_ar:
            # too wide, limit by height
            h = max_h
            w = h * target_ar
        else:
            w = max_w
            h = w / target_ar
        x0 = int(cx - w / 2)
        y0 = int(cy - h / 2)
        x1 = int(cx + w / 2)
        y1 = int(cy + h / 2)
        return BBox(x0, y0, x1, y1).clamp(img_w, img_h)

    def crop_to_aspect(self, img: Image.Image, aspect_w: int, aspect_h: int) -> Image.Image:
        W, H = img.size
        # If already close to target AR, simple letterbox crop from center
        ar_img = W / H
        ar_t = aspect_w / aspect_h
        if abs(ar_img - ar_t) < 1e-3:
            return img.copy()

        bbox = self.detect_subject_bbox(img)
        if bbox is None:
            # center crop fallback
            if ar_img > ar_t:
                # too wide
                new_w = int(H * ar_t)
                x0 = (W - new_w) // 2
                return img.crop((x0, 0, x0 + new_w, H))
            else:
                new_h = int(W / ar_t)
                y0 = (H - new_h) // 2
                return img.crop((0, y0, W, y0 + new_h))

        cx, cy = bbox.center()
        # Expand to target aspect around subject center
        crop_box = self._expand_to_aspect_around_center((cx, cy), aspect_w, aspect_h, W, H, margin_scale=1.1)
        return img.crop((crop_box.x0, crop_box.y0, crop_box.x1, crop_box.y1))

    def crop_and_resize(self, img: Image.Image, out_w: int, out_h: int) -> Image.Image:
        cropped = self.crop_to_aspect(img, out_w, out_h)
        # High-quality resampling
        return cropped.resize((out_w, out_h), Image.Resampling.LANCZOS)


# Presets with default pixel sizes
PRESETS: Dict[str, Tuple[int, int]] = {
    # Aspect presets commonly used
    "16x9": (1920, 1080),            # Landscape
    "4x5": (1080, 1350),             # Portrait (Instagram)
    "9x16": (1080, 1920),            # TikTok/Reels/Stories
    # Absolute pixel presets
    "1000x1500": (1000, 1500),       # Pinterest
    "1200x628": (1200, 628),         # Facebook link share
}


def parse_presets(presets_csv: Optional[str]) -> List[Tuple[str, Tuple[int, int]]]:
    """Parse a comma-separated list like "16x9, 1200x628" or custom sizes "800x1200".
    Returns list of (name, (w, h)). Unknown tokens that match AxB are treated as custom sizes.
    If None or empty, returns a sensible default set.
    """
    if not presets_csv:
        return [(k, PRESETS[k]) for k in ["16x9", "4x5", "9x16", "1000x1500", "1200x628"]]
    out: List[Tuple[str, Tuple[int, int]]] = []
    tokens = [t.strip().lower() for t in presets_csv.split(',') if t.strip()]

    # Common synonyms/platforms
    alias: Dict[str, str] = {
        "tiktok": "9x16",
        "reels": "9x16",
        "story": "9x16",
        "stories": "9x16",
        "portrait": "4x5",
        "instagram portrait": "4x5",
        "landscape": "16x9",
        "youtube": "16x9",
        "facebook": "1200x628",
        "pinterest": "1000x1500",
    }
    for t in tokens:
        # normalize delimiters: allow 16:9 or 16×9
        norm = t.replace('×', 'x').replace(':', 'x').replace(' ', '')
        # Map known aliases
        if norm in alias:
            norm = alias[norm]
        if norm in PRESETS:
            out.append((norm, PRESETS[norm]))
            continue
        # custom WxH
        if 'x' in norm:
            parts = norm.split('x')
            try:
                w = int(parts[0])
                h = int(parts[1])
                name = f"{w}x{h}"
                out.append((name, (w, h)))
                continue
            except Exception:
                pass
    if not out:
        out = [(k, PRESETS[k]) for k in ["16x9", "4x5", "9x16"]]
    return out
