import os
import json
from typing import Optional
from core.config import s3, s3_presign_client, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, STATIC_DIR, logger, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
import hashlib, hmac
from urllib.parse import quote, urlencode
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
    
    bucket.put_object(Key=key, Body=data, ContentType=content_type, ACL="private")

    try:
        if R2_CUSTOM_DOMAIN and (os.getenv("R2_CUSTOM_DOMAIN_BUCKET_LEVEL", "0").strip() == "1"):
            url = presign_custom_domain_bucket(key, expires_in=60 * 60)
            if url:
                return url
        if R2_CUSTOM_DOMAIN and s3_presign_client:
            return s3_presign_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET, "Key": key},
                ExpiresIn=60 * 60,
            )
        client = s3.meta.client
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=60 * 60,
        )
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
