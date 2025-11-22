"""
Mark Agent - LLM-powered function-calling agent for app actions
Uses Groq API with llama-3.3-70b-versatile for natural language understanding
"""
import os
import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
import httpx

from core.config import logger, GROQ_API_KEY
from core.auth import get_uid_from_request, resolve_workspace_uid

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


async def _groq_function_call(messages: List[Dict[str, str]], functions: List[Dict]) -> Dict[str, Any]:
    """Call Groq API with function calling support"""
    if not GROQ_API_KEY:
        return {"type": "error", "message": "Agent is not configured. Please set GROQ_API_KEY."}
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    model = os.getenv("GROQ_MODEL_AGENT", "llama-3.3-70b-versatile")
    
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": AGENT_SYSTEM_PROMPT}] + messages,
        "tools": functions,
        "tool_choice": "auto",
        "temperature": 0.1,  # Low temperature for more predictable function calling
        "max_tokens": 800,
    }
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            
            # Check if model wants to call a function
            tool_calls = message.get("tool_calls")
            if tool_calls and len(tool_calls) > 0:
                tool_call = tool_calls[0]
                function_data = tool_call.get("function", {})
                return {
                    "type": "function_call",
                    "function_name": function_data.get("name"),
                    "arguments": json.loads(function_data.get("arguments", "{}")),
                    "tool_call_id": tool_call.get("id")
                }
            
            # Regular text response
            content = message.get("content", "")
            return {
                "type": "message",
                "content": content or "I'm not sure how to help with that."
            }
            
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
    
    # Parse messages
    raw_msgs = body.get("messages")
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
    
    # Call LLM with function calling
    result = await _groq_function_call(safe_msgs, AGENT_FUNCTIONS)
    
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
