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
    return cs, bg_value, fg, border, card_bg, cap, shadow

def _render_html(payload: dict, theme: str, bg: str | None, title: str):
    cs, bg_value, fg, border, card_bg, cap, shadow = _color_theme(theme, bg)
    data_json = json.dumps(payload, ensure_ascii=False)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
    :root {{ color-scheme: {cs}; }}
    html, body {{ margin:0; padding:0; background:{bg_value}; color:{fg}; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
    .grid {{ column-count: 2; column-gap: 0; }}
    @media (min-width: 1024px) {{ .grid {{ column-count: 4; }} }}
    .card {{ display:inline-block; width:100%; margin:0; border:none; border-radius:0; overflow:hidden; background:{card_bg}; break-inside: avoid; }}
    .card img {{width: 100%;
    height: auto;         
    display: block;
    object-fit: contain;
}}
</style>
</head>
<body>
<div class="grid" id="pm-grid"></div>
<script type="application/json" id="pm-data">{data_json}</script>
<script>
(function() {{
    var dataEl = document.getElementById('pm-data');
    if(!dataEl) return;
    var DATA = JSON.parse(dataEl.textContent);
    var inIframe = (function() {{
        try {{ return window.top !== window.self; }} catch (e) {{ return true; }}
    }})();
    if (inIframe) {{
        document.documentElement.style.overflow = 'hidden';
        if (document.body) document.body.style.overflow = 'hidden';
    }} else {{
        document.documentElement.style.overflow = 'auto';
        if (document.body) document.body.style.overflow = 'auto';
    }}
    var grid = document.getElementById('pm-grid');
    if(!grid || !DATA.photos) return;
    var frag = document.createDocumentFragment();
    DATA.photos.forEach(function(p) {{
        var card = document.createElement('div');
        card.className = 'card';
        var img = document.createElement('img');
        img.loading = 'lazy';   // ✅ now lazy loading
        img.decoding = 'async';
        img.src = p.url;
        img.alt = '';
        img.addEventListener('load', sendHeight);
        img.addEventListener('error', sendHeight);
        card.appendChild(img);
        frag.appendChild(card);
    }});
    grid.appendChild(frag);
    sendHeight();

    // Auto-resize iframe height
    function sendHeight() {{
        if (!inIframe) return;
        var h = Math.max(
            document.documentElement.scrollHeight,
            document.body ? document.body.scrollHeight : 0,
    document.documentElement.offsetHeight,
            document.documentElement.clientHeight
        );
        parent.postMessage({{ type: "pm-embed-height", height: h }}, "*");
    }}
    window.addEventListener("DOMContentLoaded", sendHeight);
    window.addEventListener("load", sendHeight);
    window.addEventListener("resize", sendHeight);
    new MutationObserver(sendHeight).observe(grid, {{ childList: true, subtree: true }});
    if (typeof ResizeObserver !== "undefined") {{
        new ResizeObserver(sendHeight).observe(grid);
        if (document.body) new ResizeObserver(sendHeight).observe(document.body);
    }}
    (function(){{
        var t0 = Date.now();
        var iv = setInterval(function() {{
            sendHeight();
            if (Date.now() - t0 > 5000) clearInterval(iv);
        }}, 300);
    }})();
}})();
</script>
</body>
</html>"""

@router.get("/gallery")
def embed_gallery(
    uid: str = Query(..., min_length=3, max_length=64),
    limit: str = Query("all"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=32),
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
    bg: str | None = Query(None, min_length=1, max_length=32),
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
    bg: str | None = Query(None, min_length=1, max_length=32),
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
