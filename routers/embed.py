from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
import json, os
from datetime import datetime
from routers.photos import _build_manifest
from core.config import s3, s3_presign_client, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, STATIC_DIR as static_dir
from utils.storage import get_presigned_url
from typing import Tuple

router = APIRouter(prefix="/embed", tags=["embed"])

def _html_page(content: str) -> HTMLResponse:
    return HTMLResponse(content=content, media_type="text/html; charset=utf-8")

def _color_theme(theme: str | None, bg: str | None):
    t = (theme or "dark").lower()
    cs = "light" if t == "light" else "dark"
    if t == "light":
        bg_default, fg, border, card_bg, cap, shadow = "#ffffff", "#111111", "#dddddd", "#ffffff", "#666666", "rgba(0,0,0,0.08)"
    else:
        bg_default, fg, border, card_bg, cap, shadow = "#0b0b0b", "#dddddd", "#2b2b2b", "#1a1a1a", "#a0a0a0", "rgba(0,0,0,0.35)"
    bg_value = bg_default
    if isinstance(bg, str):
        s = bg.strip()
        if s.lower() == "transparent":
            bg_value = "transparent"
            card_bg = "transparent"
        elif s.startswith('#'):
            h = s[1:]
            if len(h) in (3, 4, 6, 8) and all(c in '0123456789abcdefABCDEF' for c in h):
                bg_value = s
                card_bg = s
        elif s.startswith('rgba(') or s.startswith('rgb('):
            # Support rgba/rgb colors from color picker
            bg_value = s
            card_bg = s
    return cs, bg_value, fg, border, card_bg, cap, shadow

def _render_html(payload: dict, theme: str, bg: str | None, title: str):
    """Render photos as true masonry layout - Bluxxy/Pinterest style with varying heights"""
    cs, bg_value, fg, border, card_bg, cap, shadow = _color_theme(theme, bg)
    photos = payload.get("photos", [])
    
    # Build photo cards
    photo_cards = ""
    for i, p in enumerate(photos):
        url = p.get("url", "")
        if url:
            photo_cards += f'<div class="c"><img src="{url}" alt="" loading="{"eager" if i < 8 else "lazy"}" decoding="async"/></div>\n'
    
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
:root{{color-scheme:{cs}}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{background:{bg_value};color:{fg};min-height:100%}}
body{{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}}

/* True masonry with CSS columns - images keep natural aspect ratio */
.g{{
    column-count:2;
    column-gap:4px;
    width:100%;
    line-height:0;
}}
@media(min-width:480px){{.g{{column-count:3;column-gap:4px}}}}
@media(min-width:768px){{.g{{column-count:4;column-gap:5px}}}}
@media(min-width:1200px){{.g{{column-count:4;column-gap:5px}}}}

/* Card - edge to edge, natural height */
.c{{
    display:inline-block;
    width:100%;
    margin:0 0 4px 0;
    overflow:hidden;
    background:#111;
    cursor:pointer;
    break-inside:avoid;
    position:relative;
}}
@media(min-width:768px){{.c{{margin-bottom:5px}}}}

.c img{{
    width:100%;
    height:auto;
    display:block;
    transition:transform .3s ease;
}}

.c:hover img{{
    transform:scale(1.02);
}}

/* Subtle overlay on hover */
.c::after{{
    content:'';
    position:absolute;
    inset:0;
    background:linear-gradient(to top,rgba(0,0,0,.15) 0%,transparent 40%);
    opacity:0;
    transition:opacity .2s;
    pointer-events:none;
}}
.c:hover::after{{opacity:1}}

/* Loading placeholder */
.c img:not([src]),.c img[src=""]{{
    background:#1a1a1a;
    min-height:200px;
}}
</style>
</head>
<body>
<div class="g">{photo_cards}</div>
<script>
(function(){{
var h=0;
function post(){{
    var nh=Math.max(document.body.scrollHeight,document.documentElement.scrollHeight);
    if(nh!==h){{h=nh;if(window.parent!==window)window.parent.postMessage({{type:'pm-embed-height',height:h}},'*');}}
}}
post();
window.addEventListener('load',post);
window.addEventListener('resize',post);
document.querySelectorAll('img').forEach(function(i){{i.onload=post}});
setInterval(post,500);
}})();
</script>
</body>
</html>"""

@router.get("/gallery")
def embed_gallery(
    uid: str = Query(..., min_length=3, max_length=64),
    limit: str = Query("all"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=64),
    keys: str | None = Query(None, min_length=1),
):
    data = _build_manifest(uid)
    photos_all = data.get("photos") or []
    if keys and keys.strip():
        desired = [k.strip() for k in keys.split(',') if k.strip()]
        lookup = {p.get("key"): p for p in photos_all}
        photos = [lookup[k] for k in desired if k in lookup]
    else:
        if limit.lower() == "all":
            photos = photos_all
        else:
            try:
                n = int(limit)
            except:
                n = 10
            photos = photos_all[:max(1, n)]
    return _html_page(_render_html({"photos": photos}, theme, bg, "Photomark Gallery"))

@router.get("/myuploads")
def embed_myuploads(
    uid: str = Query(..., min_length=3, max_length=64),
    limit: str = Query("all"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=64),
    keys: str | None = Query(None, min_length=1),
):
    items: list[dict] = []
    prefix = f"users/{uid}/external/"
    if s3 and R2_BUCKET:
        try:
            client = s3.meta.client
            continuation = None
            while True:
                params = {"Bucket": R2_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
                if continuation:
                    params["ContinuationToken"] = continuation
                resp = client.list_objects_v2(**params)
                for entry in resp.get("Contents", []) or []:
                    key = entry.get("Key", "")
                    if not key or key.endswith("/"): continue
                    name = os.path.basename(key)
                    url = get_presigned_url(key, expires_in=60 * 60) or ""
                    items.append({"key": key, "url": url, "name": name, "last": (entry.get("LastModified") or datetime.utcnow()).isoformat()})
                if resp.get("IsTruncated"):
                    continuation = resp.get("NextContinuationToken")
                else:
                    break
        except:
            items = []
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "last": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    })
    items.sort(key=lambda x: x.get("last", ""), reverse=True)
    photos_all = [{"url": it["url"], "name": it["name"], "key": it["key"]} for it in items]
    if keys and keys.strip():
        desired = [k.strip() for k in keys.split(',') if k.strip()]
        lookup = {p.get("key"): p for p in photos_all}
        photos = [lookup[k] for k in desired if k in lookup]  # ✅ correctly indented
    else:
        if limit.lower() == "all":
            photos = photos_all
        else:
            try:
                n = int(limit)
            except:
                n = 10
            photos = photos_all[:max(1, n)]
    return _html_page(_render_html({"photos": photos}, theme, bg, "Photomark My Uploads"))


def _vault_key(uid: str, vault: str) -> Tuple[str, str]:
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    if not safe:
        raise ValueError("invalid vault name")
    return f"users/{uid}/vaults/{safe}.json", safe


def _read_vault(uid: str, vault: str) -> list[str]:
    key, _ = _vault_key(uid, vault)
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            data = obj.get()["Body"].read()
            doc = json.loads(data.decode("utf-8"))
            return doc.get("keys") or []
        else:
            path = os.path.join(static_dir, key)
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
                return doc.get("keys") or []
    except Exception:
        return []


def _get_url_for_key(key: str, expires_in: int = 3600) -> str:
    if R2_CUSTOM_DOMAIN and s3_presign_client:
        return s3_presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    if s3:
        return s3.meta.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    return ""


@router.get("/vault")
def embed_vault(
    uid: str = Query(..., min_length=3, max_length=64),
    vault: str = Query(..., min_length=1, max_length=128),
    limit: str = Query("all"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=64),
    view: str = Query("grid"),
):
    """Embed a vault by name"""
    try:
        keys = _read_vault(uid, vault)
        items: list[dict] = []
        
        for key in keys:
            try:
                if key.lower().endswith('.json'):
                    continue
                url = _get_url_for_key(key, expires_in=60 * 60)
                name = os.path.basename(key)
                items.append({"key": key, "url": url, "name": name})
            except Exception:
                continue
        
        photos_all = [{"url": it["url"], "name": it["name"], "key": it["key"]} for it in items]
        
        if limit.lower() == "all":
            photos = photos_all
        else:
            try:
                n = int(limit)
            except:
                n = 10
            photos = photos_all[:max(1, n)]
        
        return _html_page(_render_html({"photos": photos}, theme, bg, f"Photomark - {vault}"))
    except Exception as ex:
        return HTMLResponse(content=f"<!doctype html><html><body><p>Error loading vault: {str(ex)}</p></body></html>", status_code=500)


def _photo_locations_key(uid: str) -> str:
    return f"users/{uid}/integrations/photo_locations.json"


def _read_photo_locations(uid: str) -> list[dict]:
    """Read photo locations from storage"""
    from utils.storage import read_json_key
    try:
        data = read_json_key(_photo_locations_key(uid)) or {}
        return data.get("photos", [])
    except Exception:
        return []


@router.get("/photo-map")
def embed_photo_map(
    uid: str = Query(..., min_length=3, max_length=64),
    style: str = Query("default"),
):
    """Embed an interactive photo map"""
    MAPBOX_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "")
    
    if not MAPBOX_TOKEN:
        return HTMLResponse(
            content="<!doctype html><html><body><p>Mapbox not configured</p></body></html>",
            status_code=500
        )
    
    # Get photo locations
    photos = _read_photo_locations(uid)
    
    # Map style mapping
    style_map = {
        "default": "streets-v12",
        "dark": "dark-v11",
        "light": "light-v11",
        "satellite": "satellite-streets-v12",
    }
    map_style = style_map.get(style, "streets-v12")
    
    # Theme colors based on style
    if style == "dark":
        bg_color, text_color, card_bg, border_color = "#1a1a2e", "#ffffff", "#16213e", "#0f3460"
    elif style == "light":
        bg_color, text_color, card_bg, border_color = "#ffffff", "#1a1a2e", "#f8f9fa", "#e9ecef"
    else:
        bg_color, text_color, card_bg, border_color = "#f8f9fa", "#1a1a2e", "#ffffff", "#dee2e6"
    
    photos_json = json.dumps(photos, ensure_ascii=False)
    
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Photo Map</title>
<link href="https://api.mapbox.com/mapbox-gl-js/v3.0.1/mapbox-gl.css" rel="stylesheet"/>
<script src="https://api.mapbox.com/mapbox-gl-js/v3.0.1/mapbox-gl.js"></script>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; overflow: hidden; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background: {bg_color}; color: {text_color}; }}
    #map {{ width: 100%; height: 100%; }}
    .photo-marker {{
        width: 48px;
        height: 48px;
        border-radius: 50%;
        border: 3px solid white;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        cursor: pointer;
        background-size: cover;
        background-position: center;
        background-color: #3b82f6;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }}
    .photo-marker:hover {{
        transform: scale(1.15);
        box-shadow: 0 6px 20px rgba(0,0,0,0.4);
    }}
    .mapboxgl-popup {{
        max-width: 320px !important;
    }}
    .mapboxgl-popup-content {{
        padding: 0 !important;
        border-radius: 16px !important;
        overflow: hidden;
        box-shadow: 0 10px 40px rgba(0,0,0,0.2) !important;
        background: {card_bg} !important;
    }}
    .mapboxgl-popup-close-button {{
        font-size: 20px;
        padding: 8px 12px;
        color: white;
        text-shadow: 0 1px 3px rgba(0,0,0,0.5);
        z-index: 10;
    }}
    .mapboxgl-popup-close-button:hover {{
        background: rgba(0,0,0,0.2);
        border-radius: 0 16px 0 8px;
    }}
    .popup-content {{
        background: {card_bg};
    }}
    .popup-image {{
        width: 100%;
        height: 180px;
        object-fit: cover;
    }}
    .popup-info {{
        padding: 16px;
    }}
    .popup-title {{
        font-weight: 600;
        font-size: 15px;
        margin-bottom: 4px;
        color: {text_color};
    }}
    .popup-coords {{
        font-size: 12px;
        color: {text_color};
        opacity: 0.6;
    }}
    .popup-date {{
        font-size: 11px;
        color: {text_color};
        opacity: 0.5;
        margin-top: 4px;
    }}
    .branding {{
        position: absolute;
        bottom: 8px;
        left: 8px;
        background: {card_bg};
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 500;
        color: {text_color};
        opacity: 0.8;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        text-decoration: none;
        transition: opacity 0.2s;
    }}
    .branding:hover {{
        opacity: 1;
    }}
    .photo-count {{
        position: absolute;
        top: 12px;
        left: 12px;
        background: {card_bg};
        padding: 8px 14px;
        border-radius: 24px;
        font-size: 13px;
        font-weight: 600;
        color: {text_color};
        box-shadow: 0 2px 12px rgba(0,0,0,0.15);
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .photo-count svg {{
        width: 16px;
        height: 16px;
        opacity: 0.7;
    }}
</style>
</head>
<body>
<div id="map"></div>
<div class="photo-count">
    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
    <span id="count">0</span> locations
</div>
<a href="https://photomark.cloud" target="_blank" class="branding">📍 Powered by Photomark</a>

<script>
(function() {{
    var PHOTOS = {photos_json};
    document.getElementById('count').textContent = PHOTOS.length;
    
    mapboxgl.accessToken = '{MAPBOX_TOKEN}';
    
    var map = new mapboxgl.Map({{
        container: 'map',
        style: 'mapbox://styles/mapbox/{map_style}',
        center: [0, 20],
        zoom: 2
    }});
    
    map.addControl(new mapboxgl.NavigationControl(), 'top-right');
    
    var markers = [];
    
    map.on('load', function() {{
        PHOTOS.forEach(function(photo) {{
            var el = document.createElement('div');
            el.className = 'photo-marker';
            if (photo.thumbnail_url) {{
                el.style.backgroundImage = 'url(' + photo.thumbnail_url + ')';
            }}
            
            var popup = new mapboxgl.Popup({{ offset: 25, closeButton: true }})
                .setHTML(
                    '<div class="popup-content">' +
                    (photo.thumbnail_url ? '<img src="' + photo.thumbnail_url + '" class="popup-image" alt=""/>' : '') +
                    '<div class="popup-info">' +
                    '<div class="popup-title">' + (photo.name || 'Photo') + '</div>' +
                    '<div class="popup-coords">' + photo.latitude.toFixed(4) + ', ' + photo.longitude.toFixed(4) + '</div>' +
                    (photo.taken_at ? '<div class="popup-date">' + new Date(photo.taken_at).toLocaleDateString() + '</div>' : '') +
                    '</div></div>'
                );
            
            var marker = new mapboxgl.Marker(el)
                .setLngLat([photo.longitude, photo.latitude])
                .setPopup(popup)
                .addTo(map);
            
            markers.push(marker);
        }});
        
        // Fit bounds if we have photos
        if (PHOTOS.length > 0) {{
            var bounds = new mapboxgl.LngLatBounds();
            PHOTOS.forEach(function(p) {{
                bounds.extend([p.longitude, p.latitude]);
            }});
            map.fitBounds(bounds, {{ padding: 60, maxZoom: 14 }});
        }}
    }});
}})();
</script>
</body>
</html>"""
    
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")
