# app.py
import os
import uuid
from typing import List, Optional, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import create_client, Client

# ---------------------
# Environment & Clients
# ---------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,https://scrumpoker-coral.vercel.app",
).split(",")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise Exception("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY env vars")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI(title="Scrum Poker API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------
# Models (backward-compatible bodies, user_id now optional/ignored if JWT present)
# ---------------------
class User(BaseModel):
    id: str
    username: Optional[str] = None
    avatar_url: Optional[str] = None

class CreateRoomRequest(BaseModel):
    user_id: Optional[str] = Field(default=None, description="Deprecated; will be taken from JWT")
    estimation_unit: Optional[str] = Field(default="points")

class CreateRoomResponse(BaseModel):
    room_id: str
    join_code: str

class JoinRoomRequest(BaseModel):
    join_code: str
    user_id: Optional[str] = Field(default=None, description="Deprecated; will be taken from JWT")

class Member(BaseModel):
    user_id: str
    username: Optional[str] = None
    avatar_url: Optional[str] = None

class JoinRoomResponse(BaseModel):
    room_id: str
    join_code: str
    members: List[Member]

class SubmitEstimateRequest(BaseModel):
    room_id: str
    estimate: float
    user_id: Optional[str] = Field(default=None, description="Deprecated; will be taken from JWT")

class RevealRequest(BaseModel):
    room_id: str
    user_id: Optional[str] = Field(default=None, description="Deprecated; will be taken from JWT")

class RevealResponse(BaseModel):
    estimates: List[Dict[str, Any]]
    average: float

class ResetRequest(BaseModel):
    room_id: str
    user_id: Optional[str] = Field(default=None, description="Deprecated; will be taken from JWT")

# ---------------------
# Auth helpers (Supabase JWT via /auth/v1/user)
# ---------------------
def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

def get_current_user_id(request: Request, fallback_user_id: Optional[str] = None) -> str:
    """
    Resolve user id from Supabase access token in Authorization header.
    Falls back to provided user_id only if no token (not recommended).
    """
    token = _extract_bearer_token(request.headers.get("Authorization"))
    if token:
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(
                    f"{SUPABASE_URL}/auth/v1/user",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            data = r.json()
            # /auth/v1/user returns the user object directly with 'id'
            uid = data.get("id")
            if not uid:
                raise HTTPException(status_code=401, detail="Invalid token payload")
            return uid
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="Auth service unavailable")
    # Backward-compat: fallback to body user_id (unsafe; avoid in production)
    if fallback_user_id:
        return fallback_user_id
    raise HTTPException(status_code=401, detail="Authorization token required")

# ---------------------
# Utilities
# ---------------------
def generate_join_code(length=6) -> str:
    return uuid.uuid4().hex[:length].upper()

def get_room_by_code(join_code: str) -> Dict[str, Any]:
    try:
        res = supabase.table("rooms").select("*").eq("join_code", join_code).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room: {str(e)}")
    if not res.data:
        raise HTTPException(status_code=404, detail="Room not found")
    # choose the first if multiple (should be unique)
    return res.data[0]

def ensure_membership(room_id: str, user_id: str) -> None:
    try:
        member = (
            supabase.table("room_users")
            .select("id")
            .eq("room_id", room_id)
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify membership: {str(e)}")
    if not member.data:
        raise HTTPException(status_code=403, detail="User not in room")

def list_members(room_id: str) -> List[Member]:
    try:
        ru = supabase.table("room_users").select("user_id").eq("room_id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch room users: {str(e)}")
    ids = [row["user_id"] for row in ru.data] if ru.data else []
    if not ids:
        return []
    try:
        users = supabase.table("users").select("id, username, avatar_url").in_("id", ids).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch user profiles: {str(e)}")
    profile_map = {u["id"]: u for u in (users.data or [])}
    members: List[Member] = []
    for uid in ids:
        prof = profile_map.get(uid, {})
        members.append(
            Member(
                user_id=uid,
                username=prof.get("username"),
                avatar_url=prof.get("avatar_url"),
            )
        )
    return members

# ---------------------
# Endpoints
# ---------------------
@app.post("/create-room", response_model=CreateRoomResponse)
def create_room(req: CreateRoomRequest, request: Request):
    user_id = get_current_user_id(request, req.user_id)
    estimation_unit = (req.estimation_unit or "points").lower()
    if estimation_unit not in ("points", "days"):
        estimation_unit = "points"

    # ensure user exists (users table populated by trigger)
    try:
        u = supabase.table("users").select("id").eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query user: {str(e)}")
    if not u.data:
        # create minimal user row if trigger didn't run yet
        try:
            supabase.table("users").insert({"id": user_id}).execute()
        except Exception:
            pass

    # unique join code
    join_code = None
    for _ in range(6):
        candidate = generate_join_code()
        chk = supabase.table("rooms").select("id").eq("join_code", candidate).execute()
        if not chk.data:
            join_code = candidate
            break
    if not join_code:
        raise HTTPException(status_code=500, detail="Could not generate unique join code")

    room_id = str(uuid.uuid4())
    try:
        supabase.table("rooms").insert(
            {
                "id": room_id,
                "join_code": join_code,
                "created_by": user_id,
                "estimation_unit": estimation_unit,
                "revealed": False,
                "average": None,
                "is_active": True,
            }
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create room: {str(e)}")

    # add creator as member (idempotent-ish)
    try:
        existing = (
            supabase.table("room_users")
            .select("id")
            .eq("room_id", room_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not existing.data:
            supabase.table("room_users").insert(
                {"id": str(uuid.uuid4()), "room_id": room_id, "user_id": user_id}
            ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add user to room: {str(e)}")

    return CreateRoomResponse(room_id=room_id, join_code=join_code)

@app.post("/join-room", response_model=JoinRoomResponse)
def join_room(req: JoinRoomRequest, request: Request):
    user_id = get_current_user_id(request, req.user_id)
    join_code = req.join_code.strip().upper()
    room = get_room_by_code(join_code)
    room_id = room["id"]
    if not room.get("is_active", True):
        raise HTTPException(status_code=403, detail="Room is not active")

    # Membership + capacity (max 15)
    try:
        existing = (
            supabase.table("room_users")
            .select("id")
            .eq("room_id", room_id)
            .eq("user_id", user_id)
            .execute()
        )
        all_members = supabase.table("room_users").select("id").eq("room_id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room users: {str(e)}")

    if (len(all_members.data or []) >= 15) and not (existing.data or []):
        raise HTTPException(status_code=403, detail="Room is full")

    if not (existing.data or []):
        try:
            supabase.table("room_users").insert(
                {"id": str(uuid.uuid4()), "room_id": room_id, "user_id": user_id}
            ).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to join room: {str(e)}")

    members = list_members(room_id)
    return JoinRoomResponse(room_id=room_id, join_code=join_code, members=members)

@app.post("/submit-estimate")
def submit_estimate(req: SubmitEstimateRequest, request: Request):
    user_id = get_current_user_id(request, req.user_id)
    room_id = req.room_id

    # Check room & membership
    try:
        r = supabase.table("rooms").select("id").eq("id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room: {str(e)}")
    if not r.data:
        raise HTTPException(status_code=404, detail="Room not found")

    ensure_membership(room_id, user_id)

    # Upsert estimate
    try:
        existing = (
            supabase.table("estimates")
            .select("id")
            .eq("room_id", room_id)
            .eq("user_id", user_id)
            .execute()
        )
        if existing.data:
            supabase.table("estimates").update({"value": req.estimate}).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("estimates").insert(
                {"id": str(uuid.uuid4()), "room_id": room_id, "user_id": user_id, "value": req.estimate}
            ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit estimate: {str(e)}")

    return {"message": "Estimate submitted successfully"}

@app.post("/reveal", response_model=RevealResponse)
def reveal(req: RevealRequest, request: Request):
    user_id = get_current_user_id(request, req.user_id)
    room_id = req.room_id

    # Room + membership (any member can reveal)
    try:
        r = supabase.table("rooms").select("*").eq("id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room: {str(e)}")
    if not r.data:
        raise HTTPException(status_code=404, detail="Room not found")

    ensure_membership(room_id, user_id)

    # Fetch estimates + user names
    try:
        est = supabase.table("estimates").select("value, user_id").eq("room_id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch estimates: {str(e)}")

    if not est.data:
        raise HTTPException(status_code=404, detail="No estimates found")

    user_ids = [row["user_id"] for row in est.data]
    try:
        users = supabase.table("users").select("id, username").in_("id", user_ids).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch users: {str(e)}")
    name_map = {u["id"]: u.get("username") for u in (users.data or [])}

    estimates_list: List[Dict[str, Any]] = []
    total, count = 0.0, 0
    for e in est.data:
        val = e.get("value")
        uname = name_map.get(e["user_id"])
        estimates_list.append({"user": uname, "estimate": val})
        if val is not None:
            total += float(val)
            count += 1

    average = float(total / count) if count > 0 else 0.0

    # Persist revealed and average
    try:
        supabase.table("rooms").update({"revealed": True, "average": average}).eq("id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update room reveal: {str(e)}")

    return RevealResponse(estimates=estimates_list, average=average)

@app.post("/reset")
def reset(req: ResetRequest, request: Request):
    user_id = get_current_user_id(request, req.user_id)
    room_id = req.room_id

    # Room + membership (any member can reset)
    try:
        r = supabase.table("rooms").select("id").eq("id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room: {str(e)}")
    if not r.data:
        raise HTTPException(status_code=404, detail="Room not found")

    ensure_membership(room_id, user_id)

    # Clear estimates and reset flags
    try:
        supabase.table("estimates").delete().eq("room_id", room_id).execute()
        supabase.table("rooms").update({"revealed": False, "average": None}).eq("id", room_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset room: {str(e)}")

    return {"message": "Room reset successfully"}

# Optional health endpoint
@app.get("/healthz")
def healthz():
    return {"ok": True}
