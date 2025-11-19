import httpx
import json
import os
from typing import List, Optional, Dict, Any
from backend.core.config import logger

# Sendbird configuration - these should be set as environment variables
SENDBIRD_APP_ID = os.getenv('SENDBIRD_APP_ID', '')
SENDBIRD_API_TOKEN = os.getenv('SENDBIRD_API_TOKEN', '')
SENDBIRD_BASE_URL = f"https://api-{SENDBIRD_APP_ID}.sendbird.com/v3"

class SendbirdAPI:
    def __init__(self):
        self.app_id = SENDBIRD_APP_ID
        self.api_token = SENDBIRD_API_TOKEN
        self.base_url = SENDBIRD_BASE_URL
        
    async def create_user(self, user_id: str, nickname: str, profile_url: str = "") -> Dict[str, Any]:
        """Create or update a Sendbird user"""
        if not self.app_id or not self.api_token:
            raise ValueError("Sendbird configuration missing")
            
        url = f"{self.base_url}/users"
        headers = {
            "Api-Token": self.api_token,
            "Content-Type": "application/json"
        }
        
        data = {
            "user_id": user_id,
            "nickname": nickname,
            "profile_url": profile_url,
            "issue_access_token": True
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, json=data)
                if response.status_code in [200, 201]:
                    return response.json()
                elif response.status_code == 400:
                    # User might already exist, try to update
                    return await self.update_user(user_id, nickname, profile_url)
                else:
                    logger.error(f"Failed to create Sendbird user: {response.status_code} - {response.text}")
                    raise Exception(f"Failed to create user: {response.status_code}")
            except httpx.RequestError as e:
                logger.error(f"Request error creating Sendbird user: {e}")
                raise Exception(f"Request error: {e}")
    
    async def update_user(self, user_id: str, nickname: str, profile_url: str = "") -> Dict[str, Any]:
        """Update a Sendbird user"""
        url = f"{self.base_url}/users/{user_id}"
        headers = {
            "Api-Token": self.api_token,
            "Content-Type": "application/json"
        }
        
        data = {
            "nickname": nickname,
            "profile_url": profile_url,
            "issue_access_token": True
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.put(url, headers=headers, json=data)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Failed to update Sendbird user: {response.status_code} - {response.text}")
                    raise Exception(f"Failed to update user: {response.status_code}")
            except httpx.RequestError as e:
                logger.error(f"Request error updating Sendbird user: {e}")
                raise Exception(f"Request error: {e}")
    
    async def create_group_channel(self, name: str, user_ids: List[str], custom_type: str = "vault", data: str = "") -> Dict[str, Any]:
        """Create a group channel for vault communication"""
        if not self.app_id or not self.api_token:
            raise ValueError("Sendbird configuration missing")
            
        url = f"{self.base_url}/group_channels"
        headers = {
            "Api-Token": self.api_token,
            "Content-Type": "application/json"
        }
        
        data_payload = {
            "name": name,
            "user_ids": user_ids,
            "custom_type": custom_type,
            "data": data,
            "is_distinct": False,  # Allow multiple channels with same users
            "is_public": False,
            "operator_ids": user_ids[:1]  # Make first user (photographer) an operator
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, json=data_payload)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Failed to create Sendbird channel: {response.status_code} - {response.text}")
                    raise Exception(f"Failed to create channel: {response.status_code}")
            except httpx.RequestError as e:
                logger.error(f"Request error creating Sendbird channel: {e}")
                raise Exception(f"Request error: {e}")
    
    async def add_users_to_channel(self, channel_url: str, user_ids: List[str]) -> Dict[str, Any]:
        """Add users to an existing channel"""
        url = f"{self.base_url}/group_channels/{channel_url}/invite"
        headers = {
            "Api-Token": self.api_token,
            "Content-Type": "application/json"
        }
        
        data = {
            "user_ids": user_ids
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, json=data)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Failed to add users to Sendbird channel: {response.status_code} - {response.text}")
                    raise Exception(f"Failed to add users to channel: {response.status_code}")
            except httpx.RequestError as e:
                logger.error(f"Request error adding users to Sendbird channel: {e}")
                raise Exception(f"Request error: {e}")
    
    async def remove_users_from_channel(self, channel_url: str, user_ids: List[str]) -> Dict[str, Any]:
        """Remove users from a channel"""
        url = f"{self.base_url}/group_channels/{channel_url}/ban"
        headers = {
            "Api-Token": self.api_token,
            "Content-Type": "application/json"
        }
        
        results = []
        async with httpx.AsyncClient() as client:
            for user_id in user_ids:
                data = {
                    "user_id": user_id,
                    "description": "Removed from vault"
                }
                
                try:
                    response = await client.post(url, headers=headers, json=data)
                    if response.status_code == 200:
                        results.append({"user_id": user_id, "success": True})
                    else:
                        logger.error(f"Failed to remove user {user_id} from Sendbird channel: {response.status_code} - {response.text}")
                        results.append({"user_id": user_id, "success": False, "error": response.text})
                except httpx.RequestError as e:
                    logger.error(f"Request error removing user {user_id} from Sendbird channel: {e}")
                    results.append({"user_id": user_id, "success": False, "error": str(e)})
        
        return {"results": results}
    
    async def get_channel_info(self, channel_url: str) -> Dict[str, Any]:
        """Get channel information"""
        url = f"{self.base_url}/group_channels/{channel_url}"
        headers = {
            "Api-Token": self.api_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Failed to get Sendbird channel info: {response.status_code} - {response.text}")
                    raise Exception(f"Failed to get channel info: {response.status_code}")
            except httpx.RequestError as e:
                logger.error(f"Request error getting Sendbird channel info: {e}")
                raise Exception(f"Request error: {e}")

# Global instance
sendbird_api = SendbirdAPI()

async def create_vault_channel(vault_name: str, photographer_id: str, client_ids: List[str]) -> Optional[str]:
    """Create a Sendbird channel for a vault"""
    try:
        # Ensure all users exist in Sendbird
        all_user_ids = [photographer_id] + client_ids
        
        # Create channel
        channel_name = f"PhotoMark - {vault_name}"
        channel_data = json.dumps({
            "vault_name": vault_name,
            "photographer_id": photographer_id,
            "type": "vault_chat"
        })
        
        result = await sendbird_api.create_group_channel(
            name=channel_name,
            user_ids=all_user_ids,
            custom_type="vault",
            data=channel_data
        )
        
        return result.get("channel_url")
    except Exception as e:
        logger.error(f"Failed to create vault channel: {e}")
        return None

async def ensure_sendbird_user(user_id: str, nickname: str, profile_url: str = "") -> Optional[str]:
    """Ensure a user exists in Sendbird and return their access token"""
    try:
        result = await sendbird_api.create_user(user_id, nickname, profile_url)
        return result.get("access_token")
    except Exception as e:
        logger.error(f"Failed to ensure Sendbird user: {e}")
        return None
