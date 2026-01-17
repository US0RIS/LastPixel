from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional
import sqlite3
import time
from datetime import datetime, timedelta
from contextlib import contextmanager
import threading
import stripe
import os

app = FastAPI(title="Pixel Canvas API")

# Stripe configuration
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

# Credit pricing
CREDITS_PER_DOLLAR = 10000  # 10,000 credits = $1
CREDIT_PACKAGES = {
    "small": {"credits": 50000, "price": 500, "name": "$5 - 50k credits"},
    "medium": {"credits": 110000, "price": 1000, "name": "$10 - 110k credits"},
    "large": {"credits": 250000, "price": 2000, "name": "$20 - 250k credits"},
}

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Constants
BOARD_SIZE = 1024
BASE_COST_CREDITS = 1000  # $0.01 in credits (10 credits = $0.01, so 1000 = $0.10 base)
COST_INCREMENT_CREDITS = 1000  # $0.01 per level
INITIAL_CAP_CREDITS = 200000  # $2.00
LOWER_CAP_CREDITS = 150000  # $1.50
CAP_TRIGGER_COUNT = 100  # pixels at cap before lowering
FREE_WINDOW_SIZE = 5000  # last N placements are free
INACTIVITY_THRESHOLD_SECONDS = 1800  # 30 minutes
FREE_ELIGIBILITY_MAX_PAID = 500  # max paid placements for free eligibility
RATE_LIMIT_SECONDS = 1  # min seconds between placements per user

# Phase 3 constants
UNDO_WINDOW_SECONDS = 300  # 5 minutes to undo
UNDO_BASE_PERCENT = 0.25  # 25% of original cost
UNDO_INCREMENT_PERCENT = 0.10  # +10% per undo
AD_SATURATION_REDUCTION = 0.5  # 50% less saturated
AD_OVERWRITE_DISCOUNT = 0.10  # 10% cheaper to overwrite
REPORT_FREEZE_THRESHOLD = 2500  # reports per week to freeze board

# In-memory rate limiting
user_last_placement = {}
rate_limit_lock = threading.Lock()

DB_PATH = "pixelcanvas.db"

# Database helper
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Initialize database
def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                credits INTEGER DEFAULT 0,
                lifetime_paid_placements INTEGER DEFAULT 0,
                undo_escalation_count INTEGER DEFAULT 0,
                ad_violation_count INTEGER DEFAULT 0,
                last_reward_month TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Pixels table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pixels (
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                color TEXT NOT NULL,
                cost_level INTEGER DEFAULT 0,
                owner_id INTEGER,
                is_ad BOOLEAN DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (x, y),
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        """)
        
        # Placements log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS placements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                color TEXT NOT NULL,
                cost INTEGER NOT NULL,
                was_free BOOLEAN DEFAULT 0,
                is_ad BOOLEAN DEFAULT 0,
                can_undo BOOLEAN DEFAULT 1,
                previous_color TEXT,
                previous_owner_id INTEGER,
                placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
        # Reports table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_user_id INTEGER NOT NULL,
                pixel_x INTEGER NOT NULL,
                pixel_y INTEGER NOT NULL,
                reason TEXT,
                reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (reporter_user_id) REFERENCES users(id)
            )
        """)
        
        # Archives table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TIMESTAMP NOT NULL,
                week_end TIMESTAMP NOT NULL,
                snapshot_data TEXT NOT NULL,
                total_placements INTEGER DEFAULT 0,
                unique_contributors INTEGER DEFAULT 0,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Votes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                archive_id INTEGER NOT NULL,
                month INTEGER NOT NULL,
                year INTEGER NOT NULL,
                voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (archive_id) REFERENCES archives(id),
                UNIQUE(user_id, month, year)
            )
        """)
        
        # Purchases table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stripe_session_id TEXT UNIQUE,
                stripe_payment_intent_id TEXT,
                amount_cents INTEGER NOT NULL,
                credits_purchased INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
        # Global state
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Initialize global state
        cursor.execute("""
            INSERT OR IGNORE INTO global_state (key, value)
            VALUES ('week_start', datetime('now'))
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO global_state (key, value)
            VALUES ('last_placement', datetime('now'))
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO global_state (key, value)
            VALUES ('current_cap', ?)
        """, (str(INITIAL_CAP_CREDITS),))
        cursor.execute("""
            INSERT OR IGNORE INTO global_state (key, value)
            VALUES ('board_frozen', '0')
        """)
        
        conn.commit()

# Request models
class PlacePixelRequest(BaseModel):
    user_id: int
    x: int = Field(..., ge=0, lt=BOARD_SIZE)
    y: int = Field(..., ge=0, lt=BOARD_SIZE)
    color: str = Field(..., pattern="^#[0-9A-Fa-f]{6}$")
    is_ad: bool = False

class BoardResponse(BaseModel):
    width: int
    height: int
    pixels: list

class PlacePixelResponse(BaseModel):
    success: bool
    cost: int
    was_free: bool
    new_balance: int
    message: str
    placement_id: Optional[int] = None

# Helper functions
def get_week_start(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_state WHERE key = 'week_start'")
    row = cursor.fetchone()
    return datetime.fromisoformat(row[0])

def get_last_placement_time(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_state WHERE key = 'last_placement'")
    row = cursor.fetchone()
    return datetime.fromisoformat(row[0])

def get_current_cap(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_state WHERE key = 'current_cap'")
    row = cursor.fetchone()
    return int(row[0])

def is_board_frozen(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM global_state WHERE key = 'board_frozen'")
    row = cursor.fetchone()
    return row[0] == '1' if row else False

def count_week_reports(conn):
    week_start = get_week_start(conn)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM reports
        WHERE reported_at >= ?
    """, (week_start.isoformat(),))
    return cursor.fetchone()[0]

def check_and_reset_week(conn):
    """Check if a week has passed and reset if needed"""
    week_start = get_week_start(conn)
    now = datetime.now()
    
    if now - week_start >= timedelta(days=7):
        cursor = conn.cursor()
        
        # Create archive snapshot before reset
        create_archive_snapshot(conn, week_start, now)
        
        # Reset all pixel cost levels
        cursor.execute("UPDATE pixels SET cost_level = 0")
        
        # Reset undo escalation for all users
        cursor.execute("UPDATE users SET undo_escalation_count = 0")
        
        # Reset week start
        cursor.execute("""
            UPDATE global_state 
            SET value = datetime('now'), updated_at = datetime('now')
            WHERE key = 'week_start'
        """)
        
        # Reset cap
        cursor.execute("""
            UPDATE global_state 
            SET value = ?, updated_at = datetime('now')
            WHERE key = 'current_cap'
        """, (str(INITIAL_CAP_CREDITS),))
        
        # Unfreeze board
        cursor.execute("""
            UPDATE global_state 
            SET value = '0', updated_at = datetime('now')
            WHERE key = 'board_frozen'
        """)
        
        conn.commit()
        return True
    return False

def create_archive_snapshot(conn, week_start, week_end):
    """Create a snapshot of the current board for archives"""
    import json
    
    cursor = conn.cursor()
    
    # Get all pixels
    cursor.execute("SELECT x, y, color, owner_id, is_ad FROM pixels")
    pixels = []
    for row in cursor.fetchall():
        pixels.append({
            "x": row[0],
            "y": row[1],
            "color": row[2],
            "owner_id": row[3],
            "is_ad": bool(row[4])
        })
    
    # Count placements this week
    cursor.execute("""
        SELECT COUNT(*), COUNT(DISTINCT user_id) FROM placements
        WHERE placed_at >= ? AND placed_at < ?
    """, (week_start.isoformat(), week_end.isoformat()))
    
    stats = cursor.fetchone()
    total_placements = stats[0]
    unique_contributors = stats[1]
    
    # Store snapshot
    cursor.execute("""
        INSERT INTO archives (week_start, week_end, snapshot_data, total_placements, unique_contributors)
        VALUES (?, ?, ?, ?, ?)
    """, (week_start.isoformat(), week_end.isoformat(), json.dumps(pixels), 
          total_placements, unique_contributors))
    
    conn.commit()

def count_week_placements(conn):
    """Count placements this week"""
    week_start = get_week_start(conn)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM placements
        WHERE placed_at >= ?
    """, (week_start.isoformat(),))
    return cursor.fetchone()[0]

def is_free_placement_eligible(conn, user_id):
    """Check if placement should be free"""
    
    # Check inactivity free mode
    last_placement = get_last_placement_time(conn)
    now = datetime.now()
    inactive_seconds = (now - last_placement).total_seconds()
    
    if inactive_seconds >= INACTIVITY_THRESHOLD_SECONDS:
        # Check user's lifetime paid placements
        cursor = conn.cursor()
        cursor.execute("""
            SELECT lifetime_paid_placements FROM users WHERE id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if row and row[0] <= FREE_ELIGIBILITY_MAX_PAID:
            return True, "inactivity"
    
    # Check last 5000 placements
    week_count = count_week_placements(conn)
    week_start = get_week_start(conn)
    
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM placements
        WHERE placed_at >= ? AND placed_at < datetime('now', '-7 days')
    """, (week_start.isoformat(),))
    
    # Simplified: if we're in the last 5000 placements of the week
    # We'll use a simpler heuristic: check total week placements
    # This is approximate but correct for Phase 1
    cursor.execute("""
        SELECT COUNT(*) FROM placements
        WHERE placed_at >= ?
    """, (week_start.isoformat(),))
    total_this_week = cursor.fetchone()[0]
    
    # Estimate end of week
    week_start_dt = get_week_start(conn)
    week_end = week_start_dt + timedelta(days=7)
    time_remaining = (week_end - now).total_seconds()
    
    # If less than certain time remains and user qualifies, could be in free window
    # For now, simplified: last 6 hours of week are free window candidate
    if time_remaining < 21600:  # 6 hours
        cursor.execute("""
            SELECT lifetime_paid_placements FROM users WHERE id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if row and row[0] <= FREE_ELIGIBILITY_MAX_PAID:
            return True, "end_of_week"
    
    return False, None

def calculate_pixel_cost(conn, x, y):
    """Calculate cost to place pixel"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cost_level, is_ad FROM pixels WHERE x = ? AND y = ?
    """, (x, y))
    row = cursor.fetchone()
    
    cost_level = row[0] if row else 0
    is_ad = bool(row[1]) if row else False
    
    base_cost = BASE_COST_CREDITS
    cost = base_cost + (cost_level * COST_INCREMENT_CREDITS // 1000)
    
    # Apply ad discount if overwriting an ad
    if is_ad:
        cost = int(cost * (1 - AD_OVERWRITE_DISCOUNT))
    
    # Apply cap
    current_cap = get_current_cap(conn)
    cost = min(cost, current_cap)
    
    return cost

def update_dynamic_cap(conn):
    """Check if cap should be lowered"""
    current_cap = get_current_cap(conn)
    
    if current_cap == INITIAL_CAP_CREDITS:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM pixels
            WHERE cost_level >= ?
        """, (current_cap // COST_INCREMENT_CREDITS * 1000,))
        
        count = cursor.fetchone()[0]
        
        if count >= CAP_TRIGGER_COUNT:
            cursor.execute("""
                UPDATE global_state
                SET value = ?, updated_at = datetime('now')
                WHERE key = 'current_cap'
            """, (str(LOWER_CAP_CREDITS),))
            conn.commit()

# API Endpoints
@app.on_event("startup")
def startup_event():
    init_db()

@app.get("/")
async def root():
    """Serve the landing page"""
    try:
        return FileResponse("index.html")
    except:
        return {"message": "Pixel Canvas API - Phase 3", "version": "0.3.0"}

@app.get("/canvas.html")
async def canvas_page():
    return FileResponse("canvas.html")

@app.get("/leaderboards.html")
async def leaderboards_page():
    return FileResponse("leaderboards.html")

@app.get("/archives.html")
async def archives_page():
    return FileResponse("archives.html")

@app.get("/board", response_model=BoardResponse)
async def get_board():
    """Get current board state"""
    with get_db() as conn:
        check_and_reset_week(conn)
        
        cursor = conn.cursor()
        cursor.execute("""
            SELECT x, y, color, cost_level, owner_id, is_ad, updated_at
            FROM pixels
            ORDER BY x, y
        """)
        
        pixels = []
        for row in cursor.fetchall():
            pixels.append({
                "x": row[0],
                "y": row[1],
                "color": row[2],
                "cost_level": row[3],
                "owner_id": row[4],
                "is_ad": bool(row[5]),
                "updated_at": row[6]
            })
        
        return BoardResponse(
            width=BOARD_SIZE,
            height=BOARD_SIZE,
            pixels=pixels
        )

@app.post("/place", response_model=PlacePixelResponse)
async def place_pixel(request: PlacePixelRequest):
    """Place a pixel on the board"""
    
    with get_db() as conn:
        # Check if board is frozen
        if is_board_frozen(conn):
            raise HTTPException(status_code=403, detail="Board is frozen due to reports")
        
        check_and_reset_week(conn)
        cursor = conn.cursor()
        
        # Validate user exists
        cursor.execute("SELECT credits, lifetime_paid_placements FROM users WHERE id = ?", 
                      (request.user_id,))
        user_row = cursor.fetchone()
        
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_credits = user_row[0]
        lifetime_paid = user_row[1]
    
    # Rate limiting check (outside transaction)
    with rate_limit_lock:
        now = time.time()
        last_time = user_last_placement.get(request.user_id, 0)
        
        if now - last_time < RATE_LIMIT_SECONDS:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit: wait {RATE_LIMIT_SECONDS} seconds between placements"
            )
        
        user_last_placement[request.user_id] = now
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check free placement eligibility
        is_free, free_reason = is_free_placement_eligible(conn, request.user_id)
        
        # Calculate cost
        cost = 0 if is_free else calculate_pixel_cost(conn, request.x, request.y)
        
        # Check sufficient credits
        cursor.execute("SELECT credits FROM users WHERE id = ?", (request.user_id,))
        user_credits = cursor.fetchone()[0]
        
        if not is_free and user_credits < cost:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient credits. Need {cost}, have {user_credits}"
            )
        
        # Get current pixel state (for undo)
        cursor.execute("""
            SELECT color, owner_id, is_ad, cost_level FROM pixels WHERE x = ? AND y = ?
        """, (request.x, request.y))
        
        existing_pixel = cursor.fetchone()
        previous_color = existing_pixel[0] if existing_pixel else None
        previous_owner = existing_pixel[1] if existing_pixel else None
        previous_is_ad = bool(existing_pixel[2]) if existing_pixel else False
        new_cost_level = (existing_pixel[3] if existing_pixel else 0) + COST_INCREMENT_CREDITS
        
        # Check for ad violation (claiming non-ad when it should be ad)
        # This is a simplified check - in production, would use ML/moderation
        if previous_is_ad and not request.is_ad:
            # User might be trying to hide an ad
            cursor.execute("""
                UPDATE users SET ad_violation_count = ad_violation_count + 1
                WHERE id = ?
            """, (request.user_id,))
        
        # Deduct credits
        if not is_free:
            cursor.execute("""
                UPDATE users
                SET credits = credits - ?,
                    lifetime_paid_placements = lifetime_paid_placements + 1
                WHERE id = ?
            """, (cost, request.user_id))
            
            new_balance = user_credits - cost
        else:
            new_balance = user_credits
        
        # Write/update pixel
        cursor.execute("""
            INSERT INTO pixels (x, y, color, cost_level, owner_id, is_ad, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(x, y) DO UPDATE SET
                color = excluded.color,
                cost_level = excluded.cost_level,
                owner_id = excluded.owner_id,
                is_ad = excluded.is_ad,
                updated_at = excluded.updated_at
        """, (request.x, request.y, request.color, new_cost_level, 
              request.user_id, request.is_ad))
        
        # Log placement (with undo capability and previous state)
        cursor.execute("""
            INSERT INTO placements (user_id, x, y, color, cost, was_free, is_ad, can_undo, 
                                   previous_color, previous_owner_id, placed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, datetime('now'))
        """, (request.user_id, request.x, request.y, request.color, 
              cost, is_free, request.is_ad, previous_color, previous_owner))
        
        placement_id = cursor.lastrowid
        
        # Update last placement time
        cursor.execute("""
            UPDATE global_state
            SET value = datetime('now'), updated_at = datetime('now')
            WHERE key = 'last_placement'
        """)
        
        conn.commit()
        
        # Update dynamic cap
        update_dynamic_cap(conn)
        
        message = "Pixel placed"
        if is_free:
            message += f" (free: {free_reason})"
        
        return PlacePixelResponse(
            success=True,
            cost=cost,
            was_free=is_free,
            new_balance=new_balance,
            message=message,
            placement_id=placement_id  # Return for undo
        )

@app.get("/user/{user_id}")
async def get_user(user_id: int):
    """Get user information"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT username, credits, lifetime_paid_placements, created_at
            FROM users WHERE id = ?
        """, (user_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "id": user_id,
            "username": row[0],
            "credits": row[1],
            "lifetime_paid_placements": row[2],
            "created_at": row[3]
        }

@app.post("/user/create")
async def create_user(username: str, initial_credits: int = 0):
    """Create a new user (for testing)"""
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO users (username, credits)
                VALUES (?, ?)
            """, (username, initial_credits))
            
            user_id = cursor.lastrowid
            conn.commit()
            
            return {
                "success": True,
                "user_id": user_id,
                "username": username,
                "credits": initial_credits
            }
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Username already exists")

@app.post("/undo/{placement_id}")
async def undo_placement(placement_id: int, user_id: int):
    """Undo a recent placement"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check if board is frozen
        if is_board_frozen(conn):
            raise HTTPException(status_code=403, detail="Board is frozen")
        
        # Get the placement
        cursor.execute("""
            SELECT user_id, x, y, cost, can_undo, placed_at, previous_color, previous_owner_id
            FROM placements WHERE id = ?
        """, (placement_id,))
        
        placement = cursor.fetchone()
        if not placement:
            raise HTTPException(status_code=404, detail="Placement not found")
        
        if placement[0] != user_id:
            raise HTTPException(status_code=403, detail="Not your placement")
        
        if not placement[4]:
            raise HTTPException(status_code=400, detail="Cannot undo this placement")
        
        # Check time window
        placed_at = datetime.fromisoformat(placement[5])
        now = datetime.now()
        if (now - placed_at).total_seconds() > UNDO_WINDOW_SECONDS:
            raise HTTPException(status_code=400, detail="Undo window expired")
        
        # Get user's undo escalation count
        cursor.execute("""
            SELECT credits, undo_escalation_count FROM users WHERE id = ?
        """, (user_id,))
        user_row = cursor.fetchone()
        
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_credits = user_row[0]
        undo_count = user_row[1]
        
        # Calculate undo cost
        original_cost = placement[3]  # cost is index 3
        undo_cost = int(original_cost * (UNDO_BASE_PERCENT + (undo_count * UNDO_INCREMENT_PERCENT)))
        
        if user_credits < undo_cost:
            raise HTTPException(status_code=402, detail=f"Insufficient credits. Need {undo_cost}")
        
        # Deduct undo cost
        cursor.execute("""
            UPDATE users 
            SET credits = credits - ?,
                undo_escalation_count = undo_escalation_count + 1
            WHERE id = ?
        """, (undo_cost, user_id))
        
        new_balance = user_credits - undo_cost
        
        # Restore previous pixel state
        x, y = placement[1], placement[2]  # x is index 1, y is index 2
        previous_color = placement[6]
        previous_owner = placement[7]
        
        if previous_color:
            # Restore previous pixel
            cursor.execute("""
                UPDATE pixels
                SET color = ?, owner_id = ?, updated_at = datetime('now')
                WHERE x = ? AND y = ?
            """, (previous_color, previous_owner, x, y))
        else:
            # Delete pixel (was empty)
            cursor.execute("DELETE FROM pixels WHERE x = ? AND y = ?", (x, y))
        
        # Mark placement as undone
        cursor.execute("""
            UPDATE placements SET can_undo = 0 WHERE id = ?
        """, (placement_id,))
        
        conn.commit()
        
        return {
            "success": True,
            "undo_cost": undo_cost,
            "new_balance": new_balance,
            "message": f"Placement undone (cost: {undo_cost} credits)"
        }

@app.post("/report")
async def report_pixel(user_id: int, x: int, y: int, reason: str = ""):
    """Report a pixel for inappropriate content"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Validate coordinates
        if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
            raise HTTPException(status_code=400, detail="Invalid coordinates")
        
        # Check if user exists
        cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        
        # Add report
        cursor.execute("""
            INSERT INTO reports (reporter_user_id, pixel_x, pixel_y, reason, reported_at)
            VALUES (?, ?, ?, ?, datetime('now'))
        """, (user_id, x, y, reason))
        
        conn.commit()
        
        # Check if threshold reached
        report_count = count_week_reports(conn)
        
        if report_count >= REPORT_FREEZE_THRESHOLD and not is_board_frozen(conn):
            # Freeze the board
            cursor.execute("""
                UPDATE global_state
                SET value = '1', updated_at = datetime('now')
                WHERE key = 'board_frozen'
            """)
            conn.commit()
            
            return {
                "success": True,
                "message": f"Report submitted. Board frozen ({report_count} reports this week)",
                "board_frozen": True,
                "report_count": report_count
            }
        
        return {
            "success": True,
            "message": "Report submitted",
            "board_frozen": False,
            "report_count": report_count
        }

@app.post("/create-checkout-session")
async def create_checkout_session(user_id: int, package: str):
    """Create a Stripe checkout session for buying credits"""
    
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    if package not in CREDIT_PACKAGES:
        raise HTTPException(status_code=400, detail="Invalid package")
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify user exists
        cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        package_info = CREDIT_PACKAGES[package]
        
        try:
            # Create Stripe checkout session
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'unit_amount': package_info['price'],
                        'product_data': {
                            'name': package_info['name'],
                            'description': f"{package_info['credits']:,} credits for PixlPlace",
                        },
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=os.environ.get('BASE_URL', 'http://localhost:5000') + '/payment-success?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=os.environ.get('BASE_URL', 'http://localhost:5000') + '/canvas.html',
                metadata={
                    'user_id': str(user_id),
                    'credits': str(package_info['credits']),
                }
            )
            
            # Record purchase in database
            cursor.execute("""
                INSERT INTO purchases (user_id, stripe_session_id, amount_cents, credits_purchased, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (user_id, checkout_session.id, package_info['price'], package_info['credits']))
            
            conn.commit()
            
            return {
                "checkout_url": checkout_session.url,
                "session_id": checkout_session.id
            }
            
        except stripe.error.StripeError as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events"""
    
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    # Handle checkout.session.completed event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Find the purchase
            cursor.execute("""
                SELECT id, user_id, credits_purchased 
                FROM purchases 
                WHERE stripe_session_id = ?
            """, (session['id'],))
            
            purchase = cursor.fetchone()
            
            if purchase:
                purchase_id, user_id, credits = purchase
                
                # Add credits to user account
                cursor.execute("""
                    UPDATE users 
                    SET credits = credits + ?
                    WHERE id = ?
                """, (credits, user_id))
                
                # Mark purchase as completed
                cursor.execute("""
                    UPDATE purchases
                    SET status = 'completed',
                        stripe_payment_intent_id = ?,
                        completed_at = datetime('now')
                    WHERE id = ?
                """, (session.get('payment_intent'), purchase_id))
                
                conn.commit()
    
    return {"status": "success"}

@app.get("/payment-success")
async def payment_success():
    """Redirect page after successful payment"""
    return FileResponse("payment-success.html")

@app.get("/leaderboard")
async def get_leaderboard(limit: int = 50):
    """Get top contributors by lifetime paid placements"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT username, lifetime_paid_placements, created_at
            FROM users
            WHERE lifetime_paid_placements > 0
            ORDER BY lifetime_paid_placements DESC
            LIMIT ?
        """, (limit,))
        
        leaderboard = []
        rank = 1
        for row in cursor.fetchall():
            leaderboard.append({
                "rank": rank,
                "username": row[0],
                "placements": row[1],
                "joined": row[2]
            })
            rank += 1
        
        return {"leaderboard": leaderboard}

@app.get("/archives")
async def get_archives():
    """Get all archived board snapshots"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, week_start, week_end, total_placements, unique_contributors, archived_at
            FROM archives
            ORDER BY week_end DESC
        """)
        
        archives = []
        for row in cursor.fetchall():
            archives.append({
                "id": row[0],
                "week_start": row[1],
                "week_end": row[2],
                "total_placements": row[3],
                "unique_contributors": row[4],
                "archived_at": row[5]
            })
        
        return {"archives": archives}

@app.get("/archives/{archive_id}")
async def get_archive(archive_id: int):
    """Get specific archive snapshot"""
    import json
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT week_start, week_end, snapshot_data, total_placements, unique_contributors
            FROM archives WHERE id = ?
        """, (archive_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Archive not found")
        
        # Get vote count for this archive
        cursor.execute("""
            SELECT COUNT(*) FROM votes WHERE archive_id = ?
        """, (archive_id,))
        vote_count = cursor.fetchone()[0]
        
        return {
            "id": archive_id,
            "week_start": row[0],
            "week_end": row[1],
            "pixels": json.loads(row[2]),
            "total_placements": row[3],
            "unique_contributors": row[4],
            "votes": vote_count
        }

@app.get("/archives/monthly/{year}/{month}")
async def get_monthly_archives(year: int, month: int):
    """Get archives from a specific month for voting"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Get archives from that month
        cursor.execute("""
            SELECT id, week_start, week_end, total_placements, unique_contributors,
                   (SELECT COUNT(*) FROM votes WHERE archive_id = archives.id) as votes
            FROM archives
            WHERE strftime('%Y', week_end) = ? AND strftime('%m', week_end) = ?
            ORDER BY week_end DESC
        """, (str(year), str(month).zfill(2)))
        
        archives = []
        for row in cursor.fetchall():
            archives.append({
                "id": row[0],
                "week_start": row[1],
                "week_end": row[2],
                "total_placements": row[3],
                "unique_contributors": row[4],
                "votes": row[5]
            })
        
        return {
            "year": year,
            "month": month,
            "archives": archives
        }

@app.post("/vote")
async def vote_for_archive(user_id: int, archive_id: int):
    """Vote for an archive in monthly voting"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check if user exists
        cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        
        # Check if archive exists
        cursor.execute("SELECT week_end FROM archives WHERE id = ?", (archive_id,))
        archive = cursor.fetchone()
        if not archive:
            raise HTTPException(status_code=404, detail="Archive not found")
        
        # Get month/year from archive
        week_end = datetime.fromisoformat(archive[0])
        month = week_end.month
        year = week_end.year
        
        # Check if user already voted this month
        try:
            cursor.execute("""
                INSERT INTO votes (user_id, archive_id, month, year)
                VALUES (?, ?, ?, ?)
            """, (user_id, archive_id, month, year))
            conn.commit()
            
            return {
                "success": True,
                "message": "Vote recorded"
            }
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Already voted this month")

@app.get("/monthly-winner/{year}/{month}")
async def get_monthly_winner(year: int, month: int):
    """Get the winner for a specific month and award credits"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Get archive with most votes from that month
        cursor.execute("""
            SELECT archives.id, archives.week_start, archives.week_end,
                   COUNT(votes.id) as vote_count
            FROM archives
            LEFT JOIN votes ON archives.id = votes.archive_id
            WHERE strftime('%Y', archives.week_end) = ? 
              AND strftime('%m', archives.week_end) = ?
            GROUP BY archives.id
            ORDER BY vote_count DESC
            LIMIT 1
        """, (str(year), str(month).zfill(2)))
        
        winner_row = cursor.fetchone()
        if not winner_row or winner_row[3] == 0:
            return {
                "year": year,
                "month": month,
                "winner": None,
                "message": "No votes yet"
            }
        
        archive_id = winner_row[0]
        vote_count = winner_row[3]
        
        # Find top contributor for that week
        week_start = winner_row[1]
        week_end = winner_row[2]
        
        cursor.execute("""
            SELECT user_id, COUNT(*) as placements
            FROM placements
            WHERE placed_at >= ? AND placed_at < ?
              AND was_free = 0
            GROUP BY user_id
            ORDER BY placements DESC
            LIMIT 1
        """, (week_start, week_end))
        
        top_contributor = cursor.fetchone()
        if not top_contributor:
            return {
                "year": year,
                "month": month,
                "archive_id": archive_id,
                "votes": vote_count,
                "winner": None,
                "message": "No paid placements"
            }
        
        winner_user_id = top_contributor[0]
        placements = top_contributor[1]
        
        # Get username
        cursor.execute("SELECT username, last_reward_month FROM users WHERE id = ?", (winner_user_id,))
        user_row = cursor.fetchone()
        username = user_row[0]
        last_reward = user_row[1]
        
        # Check 6-month cooldown
        reward_key = f"{year}-{month:02d}"
        can_receive_reward = True
        reward_given = False
        
        if last_reward:
            last_year, last_month = map(int, last_reward.split('-'))
            months_diff = (year - last_year) * 12 + (month - last_month)
            if months_diff < 6:
                can_receive_reward = False
        
        # Award credits if eligible
        reward_amount = 100000  # $1 in credits
        if can_receive_reward:
            cursor.execute("""
                UPDATE users
                SET credits = credits + ?,
                    last_reward_month = ?
                WHERE id = ?
            """, (reward_amount, reward_key, winner_user_id))
            conn.commit()
            reward_given = True
        
        return {
            "year": year,
            "month": month,
            "archive_id": archive_id,
            "votes": vote_count,
            "winner": {
                "user_id": winner_user_id,
                "username": username,
                "placements": placements,
                "reward_given": reward_given,
                "reward_amount": reward_amount if reward_given else 0,
                "cooldown_active": not can_receive_reward
            }
        }

@app.get("/stats")
async def get_stats():
    """Get global statistics"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        week_start = get_week_start(conn)
        last_placement = get_last_placement_time(conn)
        current_cap = get_current_cap(conn)
        board_frozen = is_board_frozen(conn)
        report_count = count_week_reports(conn)
        
        cursor.execute("SELECT COUNT(*) FROM pixels")
        total_pixels = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM placements
            WHERE placed_at >= ?
        """, (week_start.isoformat(),))
        week_placements = cursor.fetchone()[0]
        
        return {
            "board_size": BOARD_SIZE,
            "total_pixels_placed": total_pixels,
            "week_start": week_start.isoformat(),
            "week_placements": week_placements,
            "last_placement": last_placement.isoformat(),
            "current_cap_credits": current_cap,
            "current_cap_dollars": current_cap / 100000,
            "board_frozen": board_frozen,
            "reports_this_week": report_count,
            "report_threshold": REPORT_FREEZE_THRESHOLD
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
