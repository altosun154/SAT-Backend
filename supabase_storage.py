import os
import uuid
import requests

PROJECT_REF = os.environ.get("SUPABASE_PROJECT_REF", "rgtpylhsewekyepcgxde")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
BUCKET = "question-images"

_STORAGE_BASE = f"https://{PROJECT_REF}.supabase.co/storage/v1"


def is_configured():
    return bool(SERVICE_KEY)


def upload_image(image_bytes, ext="png", content_type="image/png"):
    """Upload image bytes to the Supabase question-images bucket under a
    'parsed/' prefix. Returns the public URL, or None if no service key is
    configured or the upload fails for any reason — callers should treat
    that as "no image available" rather than fail the whole import over a
    missing/broken bucket.
    """
    if not SERVICE_KEY:
        return None

    filename = f"parsed/{uuid.uuid4().hex}.{ext}"
    try:
        resp = requests.post(
            f"{_STORAGE_BASE}/object/{BUCKET}/{filename}",
            headers={
                "Authorization": f"Bearer {SERVICE_KEY}",
                "apikey": SERVICE_KEY,
                "Content-Type": content_type,
            },
            data=image_bytes,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    return f"https://{PROJECT_REF}.supabase.co/storage/v1/object/public/{BUCKET}/{filename}"
