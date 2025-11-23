import os
import tempfile
from typing import Any, Dict, List
from fastapi import APIRouter, Body, Request, File, UploadFile, Form
from fastapi.responses import JSONResponse
import httpx

from core.config import logger, GROQ_API_KEY
from core.auth import get_uid_from_request, get_fs_client

router = APIRouter(prefix="/api/mark", tags=["mark_assistant"])  # Global Mark chat assistant

# Optional Firestore import for server timestamps
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore

PHOTOGRAPHY_SYSTEM_PROMPT = (
    "You are Mark — a world‑class photography mentor and business coach.\n"
    "You provide precise, technically accurate and pragmatic help for:\n"
    "• Cameras (DSLR, mirrorless, cinema, medium format), sensors, dynamic range, rolling/global shutter\n"
    "• Lenses & optics (f/ T‑stops, MTF, distortion, focus breathing), focal lengths per genre\n"
    "• Exposure math (Sunny 16, EV, ND math), metering, histogram, zebras, waveforms\n"
    "• Autofocus systems, tracking modes, calibration, back‑button focus\n"
    "• Lighting (speedlights, strobes, HSS/HS, continuous, CRI/TLCI, modifiers, inverse‑square law)\n"
    "• Color management (ICC profiles, calibration, white balance, ACES, LUTs), skin‑tone strategy\n"
    "• RAW pipelines and post (Lightroom, Capture One, Photoshop), denoise, sharpening, retouching ethics\n"
    "• Video (log curves, codecs/bit‑depth/ chroma subsampling, gimbals, timecode, audio basics)\n"
    "• Workflow/tethering, culling, file‑naming, backup & archival (3‑2‑1, checksums, off‑site), delivery\n"
    "• Printing (soft‑proofing, rendering intents, paper choice), albums, color targets\n"
    "• Business (pricing, packages, licensing/usage, contracts, model/property releases, taxes, insurance)\n"
    "• Marketing (portfolio curation, positioning, SEO for photographers, lead funnels, email, ads)\n"
    "• Studio ops & on‑set safety, client experience, pre‑production planning and checklists\n"
    "Answer with concise, step‑by‑step guidance. Include concrete numbers (settings, focal lengths, powers)\n"
    "and gear examples across budgets when relevant. When assumptions are unclear, ask one incisive\n"
    "clarifying question before prescribing a solution. Tone: professional, friendly, no fluff."
)


async def _groq_chat(messages: List[Dict[str, str]], image_base64: str = None) -> str:
    if not GROQ_API_KEY:
        return "Mark is not configured. Please set GROQ_API_KEY."
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    # Use vision model if image is present, otherwise use text model
    if image_base64:
        model = "llama-3.2-90b-vision-preview"  # Vision-capable model
    else:
        model = os.getenv("GROQ_MODEL_MARK", "llama-3.1-8b-instant")
    
    # Format messages for vision model if image present
    formatted_messages = [{"role": "system", "content": PHOTOGRAPHY_SYSTEM_PROMPT}]
    
    for i, msg in enumerate(messages):
        # Only add image to the LAST user message
        is_last_user_msg = (i == len(messages) - 1 and msg.get("role") == "user")
        
        if image_base64 and is_last_user_msg:
            # For the last user message with image, use vision format
            formatted_messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": msg["content"]},
                    {"type": "image_url", "image_url": {"url": image_base64}}
                ]
            })
        else:
            formatted_messages.append(msg)
    
    payload = {
        "model": model,
        "messages": formatted_messages,
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
            return content or ""
    except Exception as ex:
        logger.warning(f"Groq request failed: {ex}")
        return "I couldn't reach the assistant service. Please try again in a moment."


async def _transcribe_audio(audio_file_path: str) -> str:
    """Transcribe audio using Groq's Whisper API"""
    if not GROQ_API_KEY:
        return "[Audio transcription unavailable]"
    
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    
    try:
        with open(audio_file_path, 'rb') as f:
            files = {'file': ('audio.webm', f, 'audio/webm')}
            data = {
                'model': 'whisper-large-v3',
                'language': 'en',
                'response_format': 'json'
            }
            
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(url, headers=headers, files=files, data=data)
                r.raise_for_status()
                result = r.json()
                return result.get('text', '').strip()
    except Exception as ex:
        logger.warning(f"Audio transcription failed: {ex}")
        return "[Could not transcribe audio]"


@router.post("/chat")
async def mark_chat(request: Request, body: Dict[str, Any] = Body(...)):
    raw_msgs = body.get("messages")
    image_data = body.get("image")  # base64 image if present
    
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return JSONResponse({"error": "messages required"}, status_code=400)

    # messages format: [{ role: 'user'|'assistant'|'system', content: string, image?: string }]
    safe_msgs: List[Dict[str, str]] = []
    has_image_in_history = False
    
    for m in raw_msgs:
        try:
            role = str(m.get("role") or "").strip()
            content = str(m.get("content") or "").strip()
            img = m.get("image")
            
            if role in ("user", "assistant") and content:
                # If there's an image, add context about it
                if img and role == "user":
                    has_image_in_history = True
                    # Add note about image for context
                    if "[Image]" in content or "📷" in content:
                        content = content.replace("📷 [Image]", "I've shared an image. ")
                    content = f"[User shared a photography image] {content}"
                
                safe_msgs.append({"role": role, "content": content})
        except Exception:
            continue
    
    if not safe_msgs:
        return JSONResponse({"error": "no valid messages"}, status_code=400)

    # Pass image to chat function if present
    reply = await _groq_chat(safe_msgs, image_base64=image_data)
    return {"reply": reply}


@router.post("/chat-audio")
async def mark_chat_audio(
    request: Request,
    audio: UploadFile = File(...),
    messages: str = Form(...)
):
    """Handle audio messages - transcribe and respond"""
    
    # Parse messages JSON
    import json
    try:
        raw_msgs = json.loads(messages)
    except Exception:
        return JSONResponse({"error": "invalid messages format"}, status_code=400)
    
    # Save audio to temporary file
    temp_file = None
    try:
        # Create temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as tmp:
            temp_file = tmp.name
            content = await audio.read()
            tmp.write(content)
        
        # Transcribe audio
        transcription = await _transcribe_audio(temp_file)
        
        if not transcription or transcription.startswith('['):
            return JSONResponse({"error": "transcription_failed"}, status_code=500)
        
        # Build message history with transcribed text
        safe_msgs: List[Dict[str, str]] = []
        for m in raw_msgs:
            try:
                role = str(m.get("role") or "").strip()
                content = str(m.get("content") or "").strip()
                if role in ("user", "assistant") and content:
                    safe_msgs.append({"role": role, "content": content})
            except Exception:
                continue
        
        # Add transcribed message
        safe_msgs.append({"role": "user", "content": transcription})
        
        # Get AI response
        reply = await _groq_chat(safe_msgs)
        
        return {
            "reply": reply,
            "transcription": transcription
        }
        
    except Exception as ex:
        logger.warning(f"Audio chat failed: {ex}")
        return JSONResponse({"error": "processing_failed"}, status_code=500)
    
    finally:
        # Clean up temp file
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except Exception:
                pass


# ---------------- Chat persistence (Firestore) ----------------

def _messages_collection(db, uid: str, chat_id: str):
    return (
        db.collection('users')
          .document(uid)
          .collection('chats')
          .document(chat_id)
          .collection('messages')
    )


@router.post("/chats/{chat_id}/messages")
async def mark_add_message(request: Request, chat_id: str, body: Dict[str, Any] = Body(...)):
    """Add a new message to a user's chat.
    Path: users/{uid}/chats/{chatId}/messages/{messageId}
    Body: { sender: 'user' | 'Mark Photography Expert', text: string }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sender = str((body or {}).get('sender') or '').strip()
    text = str((body or {}).get('text') or '').strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    # Normalize sender to allowed set
    if sender.lower() in ("user", "me"):
        sender_norm = "user"
    else:
        sender_norm = "Mark Photography Expert"

    db = get_fs_client()
    if not db or not fb_fs:
        return JSONResponse({"error": "firestore unavailable"}, status_code=503)
    try:
        doc = {
            "sender": sender_norm,
            "text": text,
            "timestamp": fb_fs.SERVER_TIMESTAMP,  # type: ignore
        }
        ref = _messages_collection(db, uid, chat_id).document()
        ref.set(doc, merge=False)
        return {"ok": True, "id": ref.id}
    except Exception as ex:
        logger.warning(f"mark_add_message failed: {ex}")
        return JSONResponse({"error": "write_failed"}, status_code=500)


@router.get("/chats/{chat_id}/messages")
async def mark_fetch_messages(request: Request, chat_id: str):
    """Fetch all messages for a chatId ordered by timestamp ascending."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = get_fs_client()
    if not db or not fb_fs:
        return JSONResponse({"error": "firestore unavailable"}, status_code=503)
    try:
        q = _messages_collection(db, uid, chat_id).order_by('timestamp', direction=fb_fs.Query.ASCENDING)  # type: ignore
        out: List[Dict[str, Any]] = []
        for snap in q.stream():  # type: ignore[attr-defined]
            data = snap.to_dict() or {}
            ts = data.get('timestamp')
            # Normalize timestamp to ISO if available
            try:
                if ts is not None and hasattr(ts, 'isoformat'):
                    data['timestamp'] = ts.isoformat()
            except Exception:
                pass
            out.append({
                "id": getattr(snap, 'id', None),
                "sender": data.get('sender') or '',
                "text": data.get('text') or '',
                "timestamp": data.get('timestamp') or None,
            })
        return {"messages": out}
    except Exception as ex:
        logger.warning(f"mark_fetch_messages failed: {ex}")
        return JSONResponse({"error": "read_failed"}, status_code=500)
