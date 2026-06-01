import httpx
from utils.config import settings

resp = httpx.post(
    "https://api.upstox.com/v2/login/authorization/token",
    data={
        "grant_type": "refresh_token",
        "refresh_token": settings.upstox_refresh_token,
        "client_id": settings.upstox_api_key,
        "client_secret": settings.upstox_api_secret,
    },
    headers={"Accept": "application/json"},
    timeout=15,
)
print("Status:", resp.status_code)
print("Response:", resp.json())
