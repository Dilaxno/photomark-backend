import os
import json
from typing import Any, Dict, List
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import httpx
import re
import io
import zipfile

from core.config import logger, GROQ_API_KEY, s3, s3_presign_client, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, STATIC_DIR as static_dir
from core.auth import resolve_workspace_uid, has_role_access
# Reuse vault helpers
from routers.vaults import (
    _read_vault,
    _write_vault,
    _read_vault_meta,
    _write_vault_meta,
    _vault_salt,
    _hash_password_bcrypt,
    _vault_key,
    _delete_vault,
)

router = APIRouter(prefix="/api/gallery/assistant", tags=["gallery_assistant"])

SYSTEM_PROMPT = (
    "You are Mark, an assistant for managing a user's photo gallery. "
    "Your output must be a JSON object only: {\\n  \"reply\": string,\\n  \"commands\": [ { \"op\": string, \"args\": object } ]\\n}. "
    "Supported command ops (use these exact op names when applicable):\\n"
    "- delete_all: {}  // Delete all photos in the user's gallery.\\n"
    "- delete_by_name: { contains: string }  // Case-insensitive substring match on filename (no path).\\n"
    "- delete_by_vault: { vault: string }  // Delete all photos inside a given vault/folder.\\n"
    "- delete_uploads: {}  // Delete all photos in the 'My uploads' tab (original uploaded photos). Use this when user says 'uploads', 'my uploads', or 'external'.\\n"
    "Guidance: Map natural language to the closest supported op. For example, 'delete all photos' => delete_all; "
    "'remove everything in wedding vault' => delete_by_vault with vault='wedding'; 'delete pictures named dog' => delete_by_name with contains='dog'; "
    "'delete all photos in uploads' or 'delete my uploads' => delete_uploads. "
    "- create_vault: { name: string, protect?: boolean, password?: string }  // Create a vault; optional password to protect.\n"
    "- add_to_vault: { vault: string, names?: [string], contains?: string }  // Add photos to a vault by names or substring match.\n"
    "- remove_vault: { vault: string }  // Remove a vault definition (does not delete physical files).\n"
    "- download_vault: { vault: string, originals?: boolean }  // Provide download links for a vault.\n"
    "- download_photos: { vault?: string, names?: [string], contains?: string, limit?: number }  // Return direct download links for matching photos.\n"
    "- search_photos: { query: string, vault?: string, limit?: number }  // Find photos by semantic/substring query (filename).\n"
    "- batch_rename: { find: string, replace: string, names?: [string], contains?: string }  // Rename photos by find/replace pattern. Optional filters: specific names or substring match.\n"
    "- rename_by_name: { old_contains: string, new_name: string }  // Rename photos matching old_contains to new_name.\n"
    "- get_info: {}  // Summarize user library: counts, vaults.\n"
    "- list_vaults: {}  // List vaults and counts.\n"
    "- open_vault: { vault: string }  // Hint UI to open a vault.\n"
    "- set_query: { query: string }  // Hint UI to filter by text.\n"
    "- set_tab: { tab: string }  // Hint UI to switch tab (gallery|uploads|vaults).\n"
    "Keep reply concise and confirm what will be done."
)

async def _groq_json(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    if not GROQ_API_KEY:
        return {"reply": "Assistant is not configured.", "commands": []}
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    # Allow override via env var, default to requested llama-3.1-8b-instant
    model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "{}")
    except Exception as ex:
        logger.warning(f"Groq request failed: {ex}")
        return {"reply": "I couldn't reach the assistant service. Please try again.", "commands": []}

    try:
        obj = json.loads(content)
    except Exception:
        obj = {"reply": str(content)[:400], "commands": []}
    if not isinstance(obj, dict):
        obj = {"reply": str(content)[:400], "commands": []}
    obj.setdefault("reply", "")
    obj.setdefault("commands", [])
    if not isinstance(obj.get("commands"), list):
        obj["commands"] = []
    return obj

async def _list_photos(uid: str, vault: str | None = None) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    base_prefix = f"users/{uid}/watermarked/"
    prefix = base_prefix if not vault else f"{base_prefix}{vault.strip('/').rstrip('/')}/"
    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if key.endswith("/_history.txt") or key.endswith("/"):
                    continue
                if R2_CUSTOM_DOMAIN and s3_presign_client:
                    url = s3_presign_client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                else:
                    url = s3.meta.client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                items.append({
                    "key": key,
                    "url": url,
                    "name": os.path.basename(key),
                    "size": getattr(obj, "size", 0),
                })
        except Exception as ex:
            logger.exception(f"list photos failed: {ex}")
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f == "_history.txt":
                        continue
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "size": os.path.getsize(local_path) if os.path.exists(local_path) else 0,
                    })
    return items

async def _list_external_photos(uid: str) -> List[Dict[str, Any]]:
    """List photos in the 'My uploads' tab - original uploaded photos before watermarking."""
    items: List[Dict[str, Any]] = []
    prefix = f"users/{uid}/external/"
    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if key.endswith("/_history.txt") or key.endswith("/"):
                    continue
                if R2_CUSTOM_DOMAIN and s3_presign_client:
                    url = s3_presign_client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                else:
                    url = s3.meta.client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                items.append({
                    "key": key,
                    "url": url,
                    "name": os.path.basename(key),
                    "size": getattr(obj, "size", 0),
                })
        except Exception as ex:
            logger.exception(f"list external photos failed: {ex}")
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f == "_history.txt":
                        continue
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "size": os.path.getsize(local_path) if os.path.exists(local_path) else 0,
                    })
    return items

async def _list_vaults(uid: str) -> List[Dict[str, Any]]:
    """List vault names and counts. Mimics /vaults endpoint logic (top-level JSON files)."""
    results: List[Dict[str, Any]] = []
    prefix = f"users/{uid}/vaults/"
    names: List[str] = []
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if not key.endswith('.json'):
                    continue
                tail = key[len(prefix):]
                if "/" in tail:
                    continue
                base = os.path.basename(key)[:-5]
                names.append(base)
        else:
            dir_path = os.path.join(static_dir, prefix)
            if os.path.isdir(dir_path):
                for f in os.listdir(dir_path):
                    if f.endswith('.json'):
                        names.append(f[:-5])
        for n in sorted(set(names)):
            try:
                count = len(_read_vault(uid, n))
            except Exception:
                count = 0
            results.append({"name": n, "count": count})
    except Exception:
        pass
    return results

async def _delete_keys(uid: str, keys: List[str]) -> Dict[str, List[str]]:
    deleted: List[str] = []
    errors: List[str] = []
    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            allowed = [k for k in keys if k.startswith(f"users/{uid}/")]
            objs = [{"Key": k} for k in allowed]
            if objs:
                resp = bucket.delete_objects(Delete={"Objects": objs, "Quiet": False})
                for d in resp.get("Deleted", []):
                    k = d.get("Key")
                    if k:
                        deleted.append(k)
                for e in resp.get("Errors", []):
                    msg = e.get("Message") or str(e)
                    key = e.get("Key")
                    errors.append(f"{key or ''}: {msg}")
        except Exception as ex:
            logger.exception(f"Delete error: {ex}")
            errors.append(str(ex))
    else:
        for k in keys:
            if not k.startswith(f"users/{uid}/"):
                errors.append(f"forbidden: {k}")
                continue
            path = os.path.join(static_dir, k)
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted.append(k)
            except Exception as ex:
                errors.append(f"{k}: {ex}")
    return {"deleted": deleted, "errors": errors}

@router.post("/chat")
async def chat(request: Request, body: Dict[str, Any]):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    raw_msgs = body.get("messages")
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return JSONResponse({"error": "messages required"}, status_code=400)
    # Optional confirmation flag for destructive actions
    confirm = bool(body.get("confirm") or False)

    # Ask Groq to turn conversation into structured commands + short reply.
    plan = await _groq_json([m for m in raw_msgs if isinstance(m, dict) and 'role' in m and 'content' in m])
    logger.info(f"assistant plan: {plan}")

    # If request implies delete_all and not confirmed yet, ask to confirm (no execution)
    commands = plan.get("commands") or []
    wants_delete_all = any((cmd or {}).get("op") == "delete_all" for cmd in commands)

    if wants_delete_all and not confirm:
        try:
            photos_preview = await _list_photos(eff_uid)
            count = len(photos_preview)
        except Exception:
            count = 0
        return {"reply": f"This will delete {count} photo(s). Confirm?", "requires_confirmation": {"op": "delete_all", "count": count}, "commands": commands}

    executed: Dict[str, Any] = {"deleted": [], "errors": [], "vault": None, "download": None, "download_links": []}

    # Execute supported commands
    try:
        commands = plan.get("commands") or []
        if isinstance(commands, list):
            # Preload photos only if needed
            photos_cache: List[Dict[str, Any]] | None = None
            for cmd in commands:
                op = (cmd or {}).get("op")
                args = (cmd or {}).get("args") or {}
                if op == "delete_all":
                    # delete every photo in gallery
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid)
                    keys = [it["key"] for it in photos_cache]
                    if keys:
                        res = await _delete_keys(eff_uid, keys)
                        executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                        executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
                elif op == "delete_by_vault":
                    vault = str(args.get("vault") or "").strip()
                    if not vault:
                        continue
                    photos_cache = await _list_photos(eff_uid, vault=vault)
                    keys = [it["key"] for it in photos_cache]
                    if keys:
                        res = await _delete_keys(eff_uid, keys)
                        executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                        executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
                elif op == "delete_uploads":
                    # Delete all photos in 'My uploads' tab (external/original uploads)
                    uploads_list = await _list_external_photos(eff_uid)
                    keys = [it["key"] for it in uploads_list]
                    if keys:
                        res = await _delete_keys(eff_uid, keys)
                        executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                        executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
                elif op == "delete_by_name":
                    contains = str(args.get("contains") or "").strip()
                    if not contains:
                        continue
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid)
                    needle = contains.lower()
                    keys = [it["key"] for it in photos_cache if needle in (it.get("name") or "").lower()]
                    if keys:
                        res = await _delete_keys(eff_uid, keys)
                        executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                        executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
                elif op == "create_vault":
                    name = str(args.get("name") or "").strip()
                    protect = bool(args.get("protect") or False)
                    password = str(args.get("password") or "").strip()
                    if not name:
                        continue
                    try:
                        keys = _read_vault(eff_uid, name)
                        _write_vault(eff_uid, name, keys)
                        if protect and password:
                            _write_vault_meta(eff_uid, name, {"protected": True, "password_hash": _hash_password_bcrypt(password)})
                        executed["vault"] = {"name": _vault_key(eff_uid, name)[1], "count": len(keys)}
                    except Exception as ex:
                        executed["errors"] = list(set(list(executed.get("errors", [])) + [str(ex)]))
                elif op == "add_to_vault":
                    vault = str(args.get("vault") or "").strip()
                    names = args.get("names") if isinstance(args.get("names"), list) else None
                    contains = str(args.get("contains") or "").strip()
                    if not vault:
                        continue
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid)
                    pool = photos_cache
                    if names:
                        want = set([str(n).lower() for n in names])
                        keys = [it["key"] for it in pool if (it.get("name") or "").lower() in want]
                    elif contains:
                        needle = contains.lower()
                        keys = [it["key"] for it in pool if needle in (it.get("name") or "").lower()]
                    else:
                        # Default: add all currently visible (or entire gallery if no vault filter)
                        keys = [it["key"] for it in pool]
                    try:
                        exist = _read_vault(eff_uid, vault)
                        filtered = [k for k in keys if k.startswith(f"users/{eff_uid}/")]
                        merged = sorted(set(exist) | set(filtered))
                        _write_vault(eff_uid, vault, merged)
                        executed["vault"] = {"name": _vault_key(eff_uid, vault)[1], "count": len(merged)}
                    except Exception as ex:
                        executed["errors"] = list(set(list(executed.get("errors", [])) + [str(ex)]))
                elif op == "remove_vault":
                    vault = str(args.get("vault") or "").strip()
                    if not vault:
                        continue
                    ok = _delete_vault(eff_uid, vault)
                    if not ok:
                        executed["errors"] = list(set(list(executed.get("errors", [])) + [f"failed to remove vault {vault}"]))
                elif op == "download_vault":
                    vault = str(args.get("vault") or "").strip()
                    originals = bool(args.get("originals") or False)
                    if not vault:
                        continue
                    # Build a zip in-memory for the requested vault (watermarked or originals)
                    try:
                        keys = _read_vault(eff_uid, vault)
                        if not keys:
                            continue
                        mem = io.BytesIO()
                        with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                            for k in keys:
                                name = os.path.basename(k)
                                # For simplicity, always fetch the watermarked key content
                                try:
                                    if s3 and R2_BUCKET:
                                        obj = s3.Object(R2_BUCKET, k)
                                        content = obj.get()["Body"].read()
                                    else:
                                        with open(os.path.join(static_dir, k), "rb") as f:
                                            content = f.read()
                                    zf.writestr(name, content)
                                except Exception:
                                    continue
                        mem.seek(0)
                        # Expose temporary URL via R2 public if available; otherwise return byte size
                        executed["download"] = {"vault": vault, "bytes": mem.getbuffer().nbytes, "originals": originals}
                    except Exception as ex:
                        executed["errors"] = list(set(list(executed.get("errors", [])) + [str(ex)]))
                elif op == "download_photos":
                    vault = str(args.get("vault") or "").strip()
                    names = args.get("names") if isinstance(args.get("names"), list) else None
                    contains = str(args.get("contains") or "").strip()
                    limit = int(args.get("limit") or 50)
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid, vault=vault or None)
                    pool = photos_cache or []
                    # Filter pool
                    if names:
                        want = set([str(n).lower() for n in names])
                        sel = [it for it in pool if (it.get("name") or "").lower() in want]
                    elif contains:
                        needle = contains.lower()
                        sel = [it for it in pool if needle in (it.get("name") or "").lower()]
                    else:
                        sel = pool
                    sel = sel[: max(0, min(200, limit))]
                    # Build direct API download links for each key
                    links = [f"/api/photos/download/{it['key']}" for it in sel if it.get('key')]
                    executed["download_links"] = links
                elif op == "search_photos":
                    query = str(args.get("query") or "").strip()
                    vault = str(args.get("vault") or "").strip()
                    limit = int(args.get("limit") or 100)
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid, vault=vault or None)
                    pool = photos_cache or []
                    ql = query.lower()
                    sel = [it for it in pool if (ql in (it.get("name") or "").lower() or ql in (it.get("key") or "").lower())]
                    sel = sel[: max(0, min(200, limit))]
                    executed["search"] = {
                        "query": query,
                        "vault": vault or None,
                        "results": [{"key": it.get("key"), "name": it.get("name"), "url": it.get("url")} for it in sel],
                        "count": len(sel),
                    }
                elif op == "get_info":
                    try:
                        total = len(await _list_photos(eff_uid))
                    except Exception:
                        total = 0
                    try:
                        vaults = await _list_vaults(eff_uid)
                    except Exception:
                        vaults = []
                    executed["info"] = {"total_photos": total, "vaults": vaults, "vault_count": len(vaults)}
                elif op == "list_vaults":
                    try:
                        executed["vaults"] = await _list_vaults(eff_uid)
                    except Exception:
                        executed["vaults"] = []
                elif op == "open_vault":
                    vname = str(args.get("vault") or "").strip()
                    ui = executed.get("ui") or {}
                    ui["open_vault"] = vname
                    executed["ui"] = ui
                elif op == "set_query":
                    q = str(args.get("query") or "").strip()
                    ui = executed.get("ui") or {}
                    ui["set_query"] = q
                    executed["ui"] = ui
                elif op == "set_tab":
                    t = str(args.get("tab") or "").strip().lower()
                    ui = executed.get("ui") or {}
                    ui["set_tab"] = t
                    executed["ui"] = ui
                elif op == "batch_rename":
                    find = str(args.get("find") or "").strip()
                    replace = str(args.get("replace") or "").strip()
                    names = args.get("names") if isinstance(args.get("names"), list) else None
                    contains = str(args.get("contains") or "").strip()
                    if not find:
                        continue
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid)
                    pool = photos_cache
                    # Filter pool if specific criteria provided
                    if names:
                        want = set([str(n).lower() for n in names])
                        sel = [it for it in pool if (it.get("name") or "").lower() in want]
                    elif contains:
                        needle = contains.lower()
                        sel = [it for it in pool if needle in (it.get("name") or "").lower()]
                    else:
                        sel = pool
                    # Perform rename via copy/delete for each item
                    renamed_count = 0
                    try:
                        for it in sel:
                            old_key = it.get("key")
                            old_name = it.get("name") or ""
                            if not old_key or find not in old_name:
                                continue
                            new_name = old_name.replace(find, replace)
                            if new_name == old_name or not new_name:
                                continue
                            # Build new key path
                            old_parts = old_key.rsplit("/", 1)
                            new_key = f"{old_parts[0]}/{new_name}" if len(old_parts) > 1 else new_name
                            # Copy and delete (S3 rename pattern)
                            try:
                                if s3 and R2_BUCKET:
                                    s3.Object(R2_BUCKET, new_key).copy_from(CopySource={"Bucket": R2_BUCKET, "Key": old_key})
                                    s3.Object(R2_BUCKET, old_key).delete()
                                else:
                                    import shutil
                                    old_path = os.path.join(static_dir, old_key)
                                    new_path = os.path.join(static_dir, new_key)
                                    if os.path.exists(old_path):
                                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                                        shutil.move(old_path, new_path)
                                renamed_count += 1
                            except Exception as ex:
                                logger.warning(f"Rename failed for {old_key}: {ex}")
                                continue
                        executed["renamed"] = {"count": renamed_count, "find": find, "replace": replace}
                    except Exception as ex:
                        executed["errors"] = list(set(list(executed.get("errors", [])) + [str(ex)]))
                elif op == "rename_by_name":
                    old_contains = str(args.get("old_contains") or "").strip()
                    new_name = str(args.get("new_name") or "").strip()
                    if not old_contains or not new_name:
                        continue
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid)
                    needle = old_contains.lower()
                    sel = [it for it in photos_cache if needle in (it.get("name") or "").lower()]
                    renamed_count = 0
                    try:
                        for it in sel:
                            old_key = it.get("key")
                            if not old_key:
                                continue
                            # Build new key path
                            old_parts = old_key.rsplit("/", 1)
                            new_key = f"{old_parts[0]}/{new_name}" if len(old_parts) > 1 else new_name
                            try:
                                if s3 and R2_BUCKET:
                                    s3.Object(R2_BUCKET, new_key).copy_from(CopySource={"Bucket": R2_BUCKET, "Key": old_key})
                                    s3.Object(R2_BUCKET, old_key).delete()
                                else:
                                    import shutil
                                    old_path = os.path.join(static_dir, old_key)
                                    new_path = os.path.join(static_dir, new_key)
                                    if os.path.exists(old_path):
                                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                                        shutil.move(old_path, new_path)
                                renamed_count += 1
                            except Exception as ex:
                                logger.warning(f"Rename failed for {old_key}: {ex}")
                                continue
                        executed["renamed"] = {"count": renamed_count, "old_contains": old_contains, "new_name": new_name}
                    except Exception as ex:
                        executed["errors"] = list(set(list(executed.get("errors", [])) + [str(ex)]))
        # Heuristic fallback if model didn't output a command
        if not executed["deleted"]:
            utext = (raw_msgs[0] or {}).get("content") if isinstance(raw_msgs[0], dict) else ""
            t = (utext or "").lower()
            if any(w in t for w in ["delete all", "remove all", "clear all", "delete everything", "remove everything"]):
                photos_cache = await _list_photos(eff_uid)
                keys = [it["key"] for it in photos_cache]
                if keys:
                    res = await _delete_keys(eff_uid, keys)
                    executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                    executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
            else:
                # try name-based fallback like "delete photos with name dog"
                m = re.search(r"name\s+([\w\-_.]+)", t)
                if not m:
                    m = re.search(r"called\s+([\w\-_.]+)", t)
                if m:
                    contains = m.group(1)
                    photos_cache = await _list_photos(eff_uid)
                    keys = [it["key"] for it in photos_cache if contains in (it.get("name") or "").lower()]
                    if keys:
                        res = await _delete_keys(eff_uid, keys)
                        executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                        executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
    except Exception as ex:
        logger.exception(f"assistant execute error: {ex}")

    reply = plan.get("reply") or "Done."
    # Return commands for client-side agents (voice Mark) and executed summary for UI updates
    return {"reply": reply, "commands": commands, "executed": executed}
