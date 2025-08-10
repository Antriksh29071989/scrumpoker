import os
import uuid
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise Exception("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY env vars")



supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()
origins = [
    "http://localhost:3000",  # or your frontend URL
    "https://your-frontend-domain.com",
    "*",  # (optional) allow all origins, but less secure
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # or ["*"] to allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
class User(BaseModel):
    id: str
    username: Optional[str]
    avatar_url: Optional[str]

class CreateRoomRequest(BaseModel):
    user_id: str

class CreateRoomResponse(BaseModel):
    room_id: str
    join_code: str

class JoinRoomRequest(BaseModel):
    join_code: str
    user_id: str

class Member(BaseModel):
    user_id: str
    username: Optional[str]
    avatar_url: Optional[str]

class JoinRoomResponse(BaseModel):
    room_id: str
    join_code: str
    members: List[Member]

class SubmitEstimateRequest(BaseModel):
    room_id: str
    user_id: str
    estimate: float

class RevealRequest(BaseModel):
    room_id: str
    user_id: str

class RevealResponse(BaseModel):
    estimates: List[dict]
    average: float

def generate_join_code(length=6):
    return uuid.uuid4().hex[:length].upper()

@app.post("/create-room", response_model=CreateRoomResponse)
def create_room(req: CreateRoomRequest):
    user_id = req.user_id

    try:
        user_res = supabase.table("users").select("id").eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query user: {str(e)}")

    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found")

    for _ in range(5):
        join_code = generate_join_code()
        try:
            code_check = supabase.table("rooms").select("*").eq("join_code", join_code).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to check join code uniqueness: {str(e)}")
        if not code_check.data:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not generate unique join code")

    room_id = str(uuid.uuid4())

    try:
        insert_room = supabase.table("rooms").insert({
            "id": room_id,
            "join_code": join_code,
            "created_by": user_id
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create room: {str(e)}")

    try:
        insert_member = supabase.table("room_users").insert({
            "id": str(uuid.uuid4()),
            "room_id": room_id,
            "user_id": user_id
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add user to room: {str(e)}")

    return CreateRoomResponse(room_id=room_id, join_code=join_code)

@app.post("/join-room", response_model=JoinRoomResponse)
def join_room(req: JoinRoomRequest):
    user_id = req.user_id
    join_code = req.join_code

    try:
        room_res = supabase.table("rooms").select("*").eq("join_code", join_code).single().execute()
        print(room_res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room: {str(e)}")

    if not room_res.data:
        raise HTTPException(status_code=404, detail="Room not found")

    room = room_res.data
    room_id = room["id"]

    try:
        existing_res = supabase.table("room_users").select("*").eq("room_id", room_id).eq("user_id", user_id).execute()
        print(existing_res)
        all_members_res = supabase.table("room_users").select("*").eq("room_id", room_id).execute()
        print(all_members_res)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query room users: {str(e)}")

    if len(all_members_res.data) >= 15 and not existing_res.data:
        raise HTTPException(status_code=403, detail="Room is full")

    if not existing_res.data:
        try:
            add_res = supabase.table("room_users").insert({
                "id": str(uuid.uuid4()),
                "room_id": room_id,
                "user_id": user_id
            }).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to join room: {str(e)}")

    try:
        members_res = supabase.table("room_users")\
            .select("user_id, users!room_users_user_id_fkey1(username, avatar_url)")\
            .eq("room_id", room_id)\
            .execute()


    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch members: {str(e)}")

    members = []
    for m in members_res.data:
        members.append(Member(
            user_id=m["user_id"],
            username=m.get("users", {}).get("username"),
            avatar_url=m.get("users", {}).get("avatar_url")
        ))

    return JoinRoomResponse(room_id=room_id, join_code=join_code, members=members)

@app.post("/submit-estimate")
def submit_estimate(req: SubmitEstimateRequest):
    user_id = req.user_id
    room_id = req.room_id
    estimate_value = req.estimate  # numeric value for estimate

    # Validate room exists
    room_res = supabase.table("rooms").select("*").eq("id", room_id).single().execute()
    if not room_res.data:
        raise HTTPException(status_code=404, detail="Room not found")

    # Check user membership in room
    member_res = supabase.table("room_users").select("*").eq("room_id", room_id).eq("user_id", user_id).execute()
    if not member_res.data:
        raise HTTPException(status_code=403, detail="User not in room")

    # Check for existing estimate by user in this room
    existing_estimate_res = supabase.table("estimates")\
        .select("*")\
        .eq("room_id", room_id)\
        .eq("user_id", user_id)\
        .execute()

    if existing_estimate_res.data and len(existing_estimate_res.data) > 0:
        existing_estimate = existing_estimate_res.data[0]
        update_res = supabase.table("estimates").update({
            "value": estimate_value
        }).eq("id", existing_estimate["id"]).execute()
        # if update_res.error:
        #     raise HTTPException(status_code=500, detail="Failed to update estimate")
    else:
        insert_res = supabase.table("estimates").insert({
            "room_id": room_id,
            "user_id": user_id,
            "value": estimate_value
        }).execute()
        # if insert_res.error:
        #     raise HTTPException(status_code=500, detail="Failed to submit estimate")

    return {"message": "Estimate submitted successfully"}

from fastapi import HTTPException

@app.get("/")
def root():
    return {"message": "Hello from the root!"}

@app.post("/reveal", response_model=RevealResponse)
def reveal(req: RevealRequest):
    user_id = req.user_id
    room_id = req.room_id

    # Check room exists
    room_res = supabase.table("rooms").select("*").eq("id", room_id).single().execute()
    if not room_res.data:
        raise HTTPException(status_code=404, detail="Room not found")

    # Check user is member of the room (any user can reveal)
    member_res = supabase.table("room_users").select("*").eq("room_id", room_id).eq("user_id", user_id).execute()
    if not member_res.data:
        raise HTTPException(status_code=403, detail="User not in room")

    # Fetch all estimates with user details
    estimates_res = supabase.table("estimates")\
        .select("value, user_id, users!estimates_user_id_fkey1(username)")\
        .eq("room_id", room_id)\
        .execute()

    if not estimates_res.data or len(estimates_res.data) == 0:
        raise HTTPException(status_code=404, detail="No estimates found")

    estimates_list = []
    total = 0
    count = 0

    for e in estimates_res.data:
        val = e.get("value")
        username = e.get("users", {}).get("username") if e.get("users") else None
        estimates_list.append({"user": username, "estimate": val})
        if val is not None:
            total += float(val)
            count += 1

    average = (total / count) if count > 0 else 0

    return RevealResponse(estimates=estimates_list, average=average)
