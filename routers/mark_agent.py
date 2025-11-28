"""
Mark Agent - LLM-powered function-calling agent for app actions
Primary model: Groq Llama 3.1 70B Versatile (function calling + vision via image_url)
Fallback: Google Gemini 2.0 Flash when GROQ_API_KEY is not configured
"""

import os
import json
import io
import base64
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, Request, UploadFile, File
from fastapi.responses import JSONResponse
import google.generativeai as genai
import httpx
from PIL import Image

from core.config import logger, GEMINI_API_KEY, GROQ_API_KEY
from core.auth import get_uid_from_request, resolve_workspace_uid

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

router = APIRouter(prefix="/api/mark/agent", tags=["mark_agent"])

# Available functions that Mark can call
AGENT_FUNCTIONS = [
    {
        "type": "function",
        "function": {
            "name": "navigate_to_page",
            "description": "Navigate to a specific page in the app",
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {
                        "type": "string",
                        "enum": ["gallery", "settings", "billing", "software", "retouch", "style-transfer", 
                                "smart-resize", "background-removal", "convert", "collaboration", "shop", 
                                "color-grading", "home"],
                        "description": "The page to navigate to"
                    }
                },
                "required": ["page"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gallery_select_all",
            "description": "Select all images in the gallery",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gallery_clear_selection",
            "description": "Clear the current selection in gallery",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gallery_delete_all",
            "description": "Delete all images from the gallery. This is a DESTRUCTIVE action that requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "Whether the user has confirmed this destructive action"
                    }
                },
                "required": ["confirmed"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "gallery_download",
            "description": "Download selected images from gallery, or all if none selected",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "uploads_select_all",
            "description": "Select all photos in My Uploads",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "uploads_delete_all",
            "description": "Delete all photos from My Uploads. This is a DESTRUCTIVE action that requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "Whether the user has confirmed this destructive action"
                    }
                },
                "required": ["confirmed"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "uploads_download",
            "description": "Download photos from My Uploads",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "account_delete",
            "description": "Permanently delete the user's account and all data. This is EXTREMELY DESTRUCTIVE and requires explicit confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "Whether the user has explicitly confirmed account deletion"
                    }
                },
                "required": ["confirmed"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "account_logout",
            "description": "Sign the user out of their account",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

AGENT_SYSTEM_PROMPT = """You are Mark, an AI assistant integrated into a photography SaaS app called Photomark.

Your job is to understand natural language requests and call the appropriate functions to help users.

IMPORTANT SAFETY RULES:
1. For DESTRUCTIVE actions (delete, remove), you MUST ask for confirmation first
2. Never call destructive functions with confirmed=true unless the user explicitly confirms
3. When user asks to delete something, first explain what will be deleted and ask "Are you sure?"
4. Only after they confirm (say "yes", "confirm", "do it", etc.) should you call with confirmed=true

CONTEXT AWARENESS:
- "gallery" = watermarked photos in the main gallery
- "uploads" = original uploaded photos before watermarking
- "my uploads" refers to the uploads section, NOT the gallery
- "account" = the entire user account and all data

Be helpful, concise, and ALWAYS prioritize safety for destructive actions."""


def _decode_base64_image(base64_str: str) -> Image.Image:
    """Decode base64 image string to PIL Image"""
    if ',' in base64_str:
        base64_str = base64_str.split(',', 1)[1]
    image_data = base64.b64decode(base64_str)
    return Image.open(io.BytesIO(image_data))


async def _gemini_function_call(messages: List[Dict[str, str]], functions: List[Dict], image_base64: str = None) -> Dict[str, Any]:
    """Call Gemini API with function calling support and image analysis"""
    if not GEMINI_API_KEY:
        return {"type": "error", "message": "Agent is not configured. Please set GEMINI_API_KEY."}
    
    try:
        # Convert function definitions to Gemini format
        gemini_tools = []
        for func in functions:
            func_def = func["function"]
            gemini_tools.append(genai.protos.Tool(
                function_declarations=[
                    genai.protos.FunctionDeclaration(
                        name=func_def["name"],
                        description=func_def["description"],
                        parameters=genai.protos.Schema(
                            type=genai.protos.Type.OBJECT,
                            properties={
                                k: genai.protos.Schema(
                                    type=genai.protos.Type.STRING if v.get("type") == "string" else genai.protos.Type.BOOLEAN,
                                    description=v.get("description", ""),
                                    enum=v.get("enum", [])
                                )
                                for k, v in func_def["parameters"]["properties"].items()
                            },
                            required=func_def["parameters"].get("required", [])
                        )
                    )
                ]
            ))
        
        model = genai.GenerativeModel('gemini-2.0-flash', tools=gemini_tools)
        
        # Build conversation with system prompt
        chat_history = []
        chat_history.append({"role": "user", "parts": [AGENT_SYSTEM_PROMPT]})
        chat_history.append({"role": "model", "parts": ["Understood. I'm Mark, your AI assistant. I can help with app navigation and actions."]})
        
        # Add message history
        for msg in messages:
            role = "model" if msg.get("role") == "assistant" else "user"
            chat_history.append({"role": role, "parts": [msg["content"]]})
        
        # Start chat
        chat = model.start_chat(history=chat_history[:-1])
        
        # Prepare final message with optional image
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
        
        # Check for function call
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    fc = part.function_call
                    return {
                        "type": "function_call",
                        "function_name": fc.name,
                        "arguments": dict(fc.args)
                    }
        
        # Regular text response
        content = response.text if response.text else "I'm not sure how to help with that."
        return {"type": "message", "content": content}
        
    except Exception as ex:
        logger.error(f"Gemini function call failed: {ex}")
        return {"type": "error", "message": "I couldn't process your request. Please try again."}

# ---- Groq (Llama 3.1 versatile) function-calling ----
async def _groq_function_call(messages: List[Dict[str, str]], functions: List[Dict], image_base64: Optional[str] = None) -> Dict[str, Any]:
    if not GROQ_API_KEY:
        return {"type": "error", "message": "Agent is not configured. Please set GROQ_API_KEY."}

    try:
        url = "https://api.groq.com/openai/v1/chat/completions"

        groq_tools = []
        for func in functions:
            groq_tools.append({
                "type": "function",
                "function": func["function"]
            })

        chat_messages: List[Dict[str, Any]] = []
        chat_messages.append({"role": "system", "content": AGENT_SYSTEM_PROMPT})

        # Add prior messages (all except the last)
        if messages:
            for m in messages[:-1]:
                chat_messages.append({"role": m["role"], "content": m["content"]})

        # Final message with optional image
        if messages:
            last = messages[-1]
            if image_base64:
                data_url = image_base64 if image_base64.startswith("data:") else f"data:image/png;base64,{image_base64}"
                chat_messages.append({
                    "role": last["role"],
                    "content": [
                        {"type": "text", "text": last["content"]},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]
                })
            else:
                chat_messages.append({"role": last["role"], "content": last["content"]})

        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": chat_messages,
            "tools": groq_tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            }, json=payload)
            r.raise_for_status()
            data = r.json()

        choice = (data.get("choices") or [{}])[0]
        msg = (choice.get("message") or {})

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0] or {}
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {}
            return {"type": "function_call", "function_name": name, "arguments": args}

        content = msg.get("content") or "I'm not sure how to help with that."
        return {"type": "message", "content": content}

    except httpx.HTTPStatusError as ex:
        try:
            detail = ex.response.json()
        except Exception:
            detail = ex.response.text
        logger.error(f"Groq upstream error: {ex.response.status_code} {detail}")
        return {"type": "error", "message": "Agent request failed."}
    except Exception as ex:
        logger.error(f"Groq function call failed: {ex}")
        return {"type": "error", "message": "I couldn't process your request. Please try again."}


def _validate_function_call(function_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """Validate that function call is safe and correct. Returns error message if invalid."""
    
    # Check if function exists
    valid_functions = [f["function"]["name"] for f in AGENT_FUNCTIONS]
    if function_name not in valid_functions:
        return f"Unknown function: {function_name}"
    
    # Validate destructive actions require confirmation
    destructive_functions = ["gallery_delete_all", "uploads_delete_all", "account_delete"]
    if function_name in destructive_functions:
        if not arguments.get("confirmed"):
            return "This is a destructive action that requires user confirmation."
    
    return None


@router.post("/chat")
async def agent_chat(request: Request, body: Dict[str, Any] = Body(...)):
    """
    Main agent endpoint that handles natural language and executes functions
    
    Request body:
    {
        "messages": [
            {"role": "user", "content": "delete all photos in my uploads"},
            {"role": "assistant", "content": "..."},
            ...
        ]
    }
    
    Response:
    {
        "type": "message" | "function_call" | "error",
        "content": "...",  // if type=message
        "function_name": "...",  // if type=function_call
        "arguments": {...},  // if type=function_call
        "requires_confirmation": true/false,  // if destructive action
        "confirmation_message": "..."  // what to show user for confirmation
    }
    """
    
    # Get user
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Parse messages and image
    raw_msgs = body.get("messages")
    image_data = body.get("image")  # base64 image if present
    
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return JSONResponse({"error": "messages required"}, status_code=400)
    
    safe_msgs: List[Dict[str, str]] = []
    for m in raw_msgs:
        try:
            role = str(m.get("role") or "").strip()
            content = str(m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                safe_msgs.append({"role": role, "content": content})
        except Exception:
            continue
    
    if not safe_msgs:
        return JSONResponse({"error": "no valid messages"}, status_code=400)
    
    # Call LLM with function calling and optional image
    if GROQ_API_KEY:
        result = await _groq_function_call(safe_msgs, AGENT_FUNCTIONS, image_base64=image_data)
    else:
        result = await _gemini_function_call(safe_msgs, AGENT_FUNCTIONS, image_base64=image_data)
    
    if result["type"] == "error":
        return {"type": "error", "message": result["message"]}
    
    if result["type"] == "message":
        return {"type": "message", "content": result["content"]}
    
    if result["type"] == "function_call":
        function_name = result["function_name"]
        arguments = result["arguments"]
        
        # Validate the function call
        validation_error = _validate_function_call(function_name, arguments)
        if validation_error:
            return {
                "type": "error",
                "message": validation_error
            }
        
        # Check if this requires confirmation
        destructive_functions = {
            "gallery_delete_all": "This will permanently delete ALL images from your gallery. This cannot be undone.",
            "uploads_delete_all": "This will permanently delete ALL photos from your uploads. This cannot be undone.",
            "account_delete": "This will permanently delete your ENTIRE ACCOUNT and all associated data. This action is irreversible."
        }
        
        if function_name in destructive_functions and not arguments.get("confirmed"):
            return {
                "type": "confirmation_required",
                "function_name": function_name,
                "arguments": arguments,
                "confirmation_message": destructive_functions[function_name],
                "message": f"⚠️ {destructive_functions[function_name]}\n\nAre you sure you want to proceed?"
            }
        
        # Function is safe to execute
        return {
            "type": "function_call",
            "function_name": function_name,
            "arguments": arguments
        }
    
    return {"type": "error", "message": "Unexpected response from agent"}


@router.post("/chat-audio")
async def agent_chat_audio(
    request: Request,
    audio: UploadFile = File(...),
    messages: str = Body(...)
):
    """Transcribe audio using Gemini and add as a user message"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not GEMINI_API_KEY:
        return JSONResponse({"error": "Gemini not configured"}, status_code=503)
    
    # Parse messages
    try:
        msgs = json.loads(messages) if isinstance(messages, str) else messages
    except Exception:
        msgs = []
    
    # Save audio file temporarily
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name
    
    try:
        # Upload and transcribe with Gemini
        audio_file = genai.upload_file(tmp_path)
        model = genai.GenerativeModel('gemini-2.0-flash')
        response = model.generate_content([
            "Transcribe this audio exactly as spoken. Only output the transcription, nothing else.",
            audio_file
        ])
        
        transcription = response.text.strip()
        
        # Clean up
        try:
            genai.delete_file(audio_file.name)
        except Exception:
            pass
        
        return {"transcription": transcription or "[Could not transcribe]"}
        
    except Exception as ex:
        logger.error(f"Audio transcription failed: {ex}")
        return JSONResponse({"error": "Transcription failed"}, status_code=500)
    
    finally:
        # Delete temp file
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
