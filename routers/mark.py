import os
import tempfile
from typing import Any, Dict, List
from fastapi import APIRouter, Body, Request, File, UploadFile, Form
from fastapi.responses import JSONResponse
import google.generativeai as genai
from PIL import Image
import io
import base64

from core.config import logger, GEMINI_API_KEY
from core.auth import get_uid_from_request, get_fs_client

router = APIRouter(prefix="/api/mark", tags=["mark_assistant"])

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

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


def _decode_base64_image(base64_str: str) -> Image.Image:
    """Decode base64 image string to PIL Image"""
    # Remove data URL prefix if present
    if ',' in base64_str:
        base64_str = base64_str.split(',', 1)[1]
    
    image_data = base64.b64decode(base64_str)
    return Image.open(io.BytesIO(image_data))


async def _gemini_chat(messages: List[Dict[str, str]], image_base64: str = None) -> str:
    """Chat with Gemini, supports text, images, and will handle audio in the same flow"""
    if not GEMINI_API_KEY:
        return "Mark is not configured. Please set GEMINI_API_KEY in your environment."
    
    try:
        # Use Gemini 1.5 Pro which supports text, images, and audio
        model = genai.GenerativeModel('gemini-1.5-pro')
        
        # Build conversation history
        chat_history = []
        
        # Add system prompt as first user message (Gemini doesn't have system role)
        chat_history.append({
            "role": "user",
            "parts": [PHOTOGRAPHY_SYSTEM_PROMPT]
        })
        chat_history.append({
            "role": "model",
            "parts": ["Understood. I'm Mark, your photography expert. I'll provide precise, technical guidance on all aspects of photography."]
        })
        
        # Add message history
        for msg in messages:
            role = "model" if msg.get("role") == "assistant" else "user"
            chat_history.append({
                "role": role,
                "parts": [msg["content"]]
            })
        
        # Start chat with history
        chat = model.start_chat(history=chat_history[:-1])  # Exclude last message
        
        # Prepare the final message (potentially with image)
        final_parts = []
        
        if messages:
            final_parts.append(messages[-1]["content"])
        
        # Add image if present
        if image_base64:
            try:
                pil_image = _decode_base64_image(image_base64)
                final_parts.append(pil_image)
            except Exception as img_ex:
                logger.warning(f"Failed to decode image: {img_ex}")
        
        # Send message
        response = chat.send_message(final_parts)
        return response.text
        
    except Exception as ex:
        logger.warning(f"Gemini request failed: {ex}")
        return "I couldn't process that. Please try again in a moment."


async def _transcribe_audio_gemini(audio_file_path: str) -> str:
    """Transcribe audio using Gemini's native audio support"""
    if not GEMINI_API_KEY:
        return "[Audio transcription unavailable]"
    
    try:
        # Upload audio file to Gemini
        audio_file = genai.upload_file(audio_file_path)
        
        # Use Gemini to transcribe
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content([
            "Please transcribe this audio exactly as spoken. Only provide the transcription, no additional commentary.",
            audio_file
        ])
        
        # Delete the uploaded file
        audio_file.delete()
        
        return response.text.strip()
        
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
    reply = await _gemini_chat(safe_msgs, image_base64=image_data)
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
        transcription = await _transcribe_audio_gemini(temp_file)
        
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
        reply = await _gemini_chat(safe_msgs)
        
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
