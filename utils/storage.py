import os
import json
from typing import Optional
from core.config import s3, s3_presign_client, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, STATIC_DIR, logger, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, s3_backup, BACKUP_BUCKET
import hashlib, hmac
from urllib.parse import quote, urlencode
import time

# Simple in-process cache for presigned URLs
_URL_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL = int(os.getenv("URL_CACHE_TTL_SEC", "300") or "300")
from botocore.exceptions import ClientError


def write_json_key(key: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=data.encode('utf-8'), ContentType='application/json', ACL='private')
    else:
        path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data)


def read_json_key(key: str) -> Optional[dict]:
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            try:
                body = obj.get()["Body"].read().decode("utf-8")
            except ClientError as ce:
                # Treat missing object as None without warning noise
                if ce.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                    return None
                raise
            return json.loads(body)
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as ex:
        logger.warning(f"read_json_key failed for {key}: {ex}")
        return None


def upload_bytes(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    if not s3 or not R2_BUCKET:
        local_path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        logger.info(f"Saved locally: {local_path}")
        return f"/static/{key}"

    bucket = s3.Bucket(R2_BUCKET)

    try:
        cc = "public, max-age=604800"
        k = (key or "").strip()
        if "/watermarked/" in k:
            cc = "public, max-age=2592000"
        bucket.put_object(Key=key, Body=data, ContentType=content_type, ACL="private", CacheControl=cc)
    except Exception:
        bucket.put_object(Key=key, Body=data, ContentType=content_type, ACL="private")

    # Attempt backup mirror (best-effort; non-blocking on failure)
    try:
        if s3_backup and BACKUP_BUCKET:
            try:
                s3_backup.Bucket(BACKUP_BUCKET).put_object(Key=key, Body=data, ContentType=content_type, ACL="private")
                logger.info(f"Backup mirrored to B2: {BACKUP_BUCKET}/{key}")
            except Exception as bx:
                logger.warning(f"Backup mirror failed for {key}: {bx}")
    except Exception:
        pass

    try:
        url = get_presigned_url(key, expires_in=60 * 60)
        if url:
            return url
        return f"/static/{key}"
    except Exception as ex:
        logger.warning(f"presigned url generation failed for {key}: {ex}")
        return f"/static/{key}"


def presign_custom_domain_bucket(key: str, expires_in: int = 3600) -> str:
    try:
        domain = (R2_CUSTOM_DOMAIN or "").strip()
        access_key = (R2_ACCESS_KEY_ID or "").strip()
        secret_key = (R2_SECRET_ACCESS_KEY or "").strip()
        bucket = (R2_BUCKET or "").strip()
        if not (domain and access_key and secret_key and bucket and key):
            return ""

        method = "GET"
        service = "s3"
        region = "auto"
        from datetime import datetime
        now = datetime.utcnow()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        credential = f"{access_key}/{credential_scope}"

        host = domain
        signed_headers = "host"
        canonical_uri = "/" + quote(str(key).lstrip("/"), safe="/")

        q = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(int(expires_in or 3600)),
            "X-Amz-SignedHeaders": signed_headers,
        }
        canonical_querystring = urlencode(q, safe="/", quote_via=lambda s, *_: quote(s, safe="/"))

        canonical_headers = f"host:{host}\n"
        payload_hash = hashlib.sha256(b"").hexdigest()

        canonical_request = "\n".join([
            method,
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])

        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        def _sign(key_bytes: bytes, msg: str) -> bytes:
            return hmac.new(key_bytes, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
        k_region = _sign(k_date, region)
        k_service = _sign(k_region, service)
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        final_qs = canonical_querystring + "&X-Amz-Signature=" + signature
        return f"https://{host}{canonical_uri}?{final_qs}"
    except Exception as ex:
        logger.warning(f"custom-domain presign failed for {key}: {ex}")
        return ""


def get_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Central helper: returns cached presigned URL if available, otherwise generates.
    Supports bucket-level custom domains via custom signer and standard presign client.
    """
    try:
        k = f"{key}|{int(expires_in)}"
        now = time.time()
        cached = _URL_CACHE.get(k)
        if cached and cached[1] > now:
            return cached[0]

        url = ""
        if R2_CUSTOM_DOMAIN and (os.getenv("R2_CUSTOM_DOMAIN_BUCKET_LEVEL", "0").strip() == "1"):
            url = presign_custom_domain_bucket(key, expires_in=expires_in)
        elif R2_CUSTOM_DOMAIN and s3_presign_client:
            url = s3_presign_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET, "Key": key},
                ExpiresIn=expires_in,
            )
        elif s3:
            url = s3.meta.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET, "Key": key},
                ExpiresIn=expires_in,
            )

        if url:
            _URL_CACHE[k] = (url, now + max(1, min(_CACHE_TTL, int(expires_in))))
        return url
    except Exception as ex:
        logger.warning(f"get_presigned_url failed for {key}: {ex}")
        return ""


def read_bytes_key(key: str) -> Optional[bytes]:
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            body = obj.get()["Body"].read()
            return body
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return None
            with open(path, "rb") as f:
                return f.read()
    except Exception as ex:
        logger.warning(f"read_bytes_key failed for {key}: {ex}")
        return None


def backup_read_bytes_key(key: str) -> Optional[bytes]:
    try:
        if s3_backup and BACKUP_BUCKET:
            obj = s3_backup.Object(BACKUP_BUCKET, key)
            body = obj.get()["Body"].read()
            return body
        return None
    except Exception as ex:
        logger.warning(f"backup_read_bytes_key failed for {key}: {ex}")
        return None


def backup_delete_key(key: str) -> bool:
    try:
        if s3_backup and BACKUP_BUCKET:
            s3_backup.Object(BACKUP_BUCKET, key).delete()
            return True
        return False
    except Exception as ex:
        logger.warning(f"backup_delete_key failed for {key}: {ex}")
        return False


def list_keys(prefix: str, max_keys: int = 1000) -> list[str]:
    """List all keys with a given prefix in the bucket."""
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            keys = []
            for obj in bucket.objects.filter(Prefix=prefix).limit(max_keys):
                keys.append(obj.key)
            return keys
        else:
            # Local filesystem fallback
            local_dir = os.path.join(STATIC_DIR, prefix)
            if not os.path.isdir(local_dir):
                return []
            keys = []
            for root, _, files in os.walk(local_dir):
                for f in files:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, STATIC_DIR)
                    keys.append(rel_path.replace("\\", "/"))
                    if len(keys) >= max_keys:
                        return keys
            return keys
    except Exception as ex:
        logger.warning(f"list_keys failed for prefix {prefix}: {ex}")
        return []
