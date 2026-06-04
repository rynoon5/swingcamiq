import os
import base64
import subprocess
import tempfile
import json
import re
import uuid
import sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import anthropic
import stripe

app = FastAPI(title="SwingCamIQ API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Stripe config ──────────────────────────────────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

STRIPE_PRICE_IDS = {
    "pack":    os.environ.get("STRIPE_PRICE_PACK",    "price_1TejKEQUZDUQHafGLnajWPc1"),
    "monthly": os.environ.get("STRIPE_PRICE_MONTHLY", "price_1TejNWQUZDUQHafGnVDYGgTv"),
    "annual":  os.environ.get("STRIPE_PRICE_ANNUAL",  "price_1TejOxQUZDUQHafGoGMLRwK4"),
}

STRIPE_PLAN_USES = {
    "pack": 3,
    "monthly": 9999,
    "annual": 9999,
}

# ── Directory setup ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
# On Railway, use /data for persistent storage if available, else local
DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", str(BASE_DIR)))
DB_PATH = DATA_DIR / "swingcamiq.db"
FRAMES_DIR = DATA_DIR / "frames"
FRAMES_DIR.mkdir(parents=True, exist_ok=True)

# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            golfer_name TEXT,
            skill_level TEXT,
            club_type TEXT,
            camera_angle TEXT,
            common_miss TEXT,
            overall_score INTEGER,
            overall_rating TEXT,
            handicap_estimate TEXT,
            headline TEXT,
            full_result TEXT,
            frame_count INTEGER DEFAULT 0,
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            free_uses_used INTEGER DEFAULT 0,
            free_uses_limit INTEGER DEFAULT 2,
            plan TEXT DEFAULT 'free',
            plan_activated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            plan_interest TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

init_db()

FREE_LIMIT = 2

# ── Usage helpers ──────────────────────────────────────────────────────────────
def get_or_create_user(conn, email: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (email, created_at, free_uses_used, free_uses_limit, plan) VALUES (?,?,0,?,?)",
            (email, datetime.utcnow().isoformat(), FREE_LIMIT, "free")
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row)

def user_can_analyze(user: dict) -> bool:
    if user["plan"] in ("pack", "monthly", "annual"):
        return True
    return user["free_uses_used"] < user["free_uses_limit"]

def uses_remaining(user: dict) -> int:
    if user["plan"] in ("pack", "monthly", "annual"):
        return 999
    return max(0, user["free_uses_limit"] - user["free_uses_used"])


# ── Static files ───────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Serve frames from DATA_DIR (works both locally and on Railway)
from fastapi.responses import FileResponse as FR
@app.get("/frames/{session_id}/{filename}")
def serve_frame(session_id: str, filename: str):
    path = FRAMES_DIR / session_id / filename
    if not path.exists():
        raise HTTPException(404, "Frame not found")
    return FR(str(path), media_type="image/jpeg")

@app.get("/static/manifest.json")
def manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"))

@app.get("/static/sw.js")
def service_worker():
    return FileResponse(str(STATIC_DIR / "sw.js"), media_type="application/javascript")

@app.get("/")
def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Usage check endpoint ───────────────────────────────────────────────────────
@app.get("/usage")
def check_usage(email: str = ""):
    """Check how many free analyses a user has left."""
    if not email.strip():
        return {"plan": "free", "uses_remaining": FREE_LIMIT, "can_analyze": True, "is_new": True}
    conn = get_db()
    user = get_or_create_user(conn, email.strip().lower())
    conn.close()
    return {
        "plan": user["plan"],
        "uses_remaining": uses_remaining(user),
        "can_analyze": user_can_analyze(user),
        "is_new": user["free_uses_used"] == 0,
    }


# ── Waitlist endpoint ──────────────────────────────────────────────────────────
@app.post("/waitlist")
async def join_waitlist(body: dict):
    email = body.get("email", "").strip().lower()
    plan = body.get("plan", "").strip()
    if not email:
        raise HTTPException(400, "Email required")
    conn = get_db()
    # Upsert user record
    get_or_create_user(conn, email)
    # Record waitlist interest
    conn.execute(
        "INSERT INTO waitlist (email, plan_interest, created_at) VALUES (?,?,?)",
        (email, plan, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "You're on the list!"}


# ── Admin: waitlist view (simple) ──────────────────────────────────────────────
@app.get("/admin/waitlist")
def admin_waitlist():
    conn = get_db()
    rows = conn.execute("""
        SELECT w.email, w.plan_interest, w.created_at,
               u.free_uses_used, u.plan
        FROM waitlist w
        LEFT JOIN users u ON u.email = w.email
        ORDER BY w.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]




# ── Stripe checkout ────────────────────────────────────────────────────────────
@app.post("/create-checkout-session")
async def create_checkout_session(body: dict):
    email = body.get("email", "").strip().lower()
    plan  = body.get("plan", "monthly").strip()

    if not email:
        raise HTTPException(400, "Email required")
    if plan not in STRIPE_PRICE_IDS:
        raise HTTPException(400, "Invalid plan")

    price_id = STRIPE_PRICE_IDS[plan]
    base_url = body.get("base_url", "https://web-production-12512.up.railway.app")

    mode = "subscription" if plan in ("monthly", "annual") else "payment"

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode=mode,
        customer_email=email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base_url}/payment-success?session_id={{CHECKOUT_SESSION_ID}}&email={email}&plan={plan}",
        cancel_url=f"{base_url}/?cancelled=1",
        metadata={"email": email, "plan": plan},
    )
    return {"url": session.url}


@app.get("/payment-success")
async def payment_success(session_id: str, email: str, plan: str):
    """Called after successful Stripe payment — unlock user account."""
    try:
        # Verify with Stripe
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status in ("paid", "no_payment_required"):
            conn = get_db()
            user = get_or_create_user(conn, email.lower())
            uses = STRIPE_PLAN_USES.get(plan, 9999)
            conn.execute("""
                UPDATE users SET plan=?, free_uses_used=0, free_uses_limit=?, plan_activated_at=?
                WHERE email=?
            """, (plan, uses, datetime.utcnow().isoformat(), email.lower()))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"Payment success error: {e}")

    # Redirect to app
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/webhook")
async def stripe_webhook(request):
    """Stripe webhook for reliable payment confirmation."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        else:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    except Exception as e:
        raise HTTPException(400, str(e))

    if event.type in ("checkout.session.completed", "payment_intent.succeeded"):
        session = event.data.object
        email = session.get("customer_email") or session.metadata.get("email", "")
        plan  = session.metadata.get("plan", "monthly")
        if email:
            conn = get_db()
            get_or_create_user(conn, email.lower())
            uses = STRIPE_PLAN_USES.get(plan, 9999)
            conn.execute("""
                UPDATE users SET plan=?, free_uses_used=0, free_uses_limit=?, plan_activated_at=?
                WHERE email=?
            """, (plan, uses, datetime.utcnow().isoformat(), email.lower()))
            conn.commit()
            conn.close()

    return {"status": "ok"}


# ── Prompts ────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are SwingCamIQ, a warm and knowledgeable golf instructor with 20+ years of experience coaching everyday golfers of all skill levels. You've seen every fault, every miss, every frustration — and you know how to explain fixes in plain language that actually makes sense on the range.

Your job is to analyze golf swing images and give feedback that is:
- HONEST but encouraging — not harsh, not sugarcoating
- PLAIN LANGUAGE — no jargon without explanation. Say "your hips spin too early" not "premature pelvis rotation causes path deviation"
- SPECIFIC — tell them exactly what you see, not vague platitudes
- ACTIONABLE — every fault gets a feel cue AND a drill they can do today
- PRIORITIZED — focus on the 1-2 things that will make the biggest difference, not 10 things at once

BALL FLIGHT LAWS (use these to connect miss patterns to swing causes):
- SLICE (curves right for RH golfer): open clubface relative to swing path. Caused by: over-the-top path, weak grip, cupped lead wrist, casting.
- HOOK (curves hard left): closed face relative to path. Caused by: too strong grip, flipping/rolling forearms, excessive in-to-out path.
- PULL (goes straight left): face square but path is out-to-in. Often paired with slice.
- PUSH (goes straight right): face square but path is in-to-out too much.
- FADE (gentle left-to-right): slightly open face to path — can be intentional.
- DRAW (gentle right-to-left): slightly closed face to path — often a power shape.
- FAT (hits ground before ball): weight hanging back, ball too far forward, early extension, scooping.
- THIN (hits top of ball): standing up through impact, head lifting, trying to "help" ball airborne.
- TOPPED: extreme version of thin — head lifting, spine extending upward.
- SHANK (hosel strike): arms too far from body at impact, early extension pushing hands toward ball.

SWING PHASES — what to look for:

ADDRESS/SETUP:
✓ Feet shoulder-width apart
✓ Spine tilted 20-30° from waist (athletic posture, not hunched or too upright)
✓ Knees slightly flexed (not squatting)
✓ Weight 50/50, ball position varies by club
✓ Arms hanging naturally, not reaching or cramped
✓ Club face square to target

TAKEAWAY (club to waist):
✓ One-piece move: shoulders, arms, hands move together
✓ Club face angle mirrors spine angle at waist height (toe pointing up)
✓ No rolling/fanning the face open
✓ No picking the club up steeply with hands

BACKSWING:
✓ Shoulder turn 80-100° for men
✓ Hip turn ~45° (creating "X-factor" coil)
✓ Lead arm relatively straight (not rigid)
✓ Weight loading to trail foot (60-70% at top)
✓ Spine angle maintained — no sway (lateral slide) or dip

TOP OF BACKSWING:
✓ Club shaft roughly parallel to ground (slightly short is fine)
✓ Lead wrist flat or bowed — NOT cupped (cupped = open face = slice)
✓ Club face parallel to lead forearm
✓ Fully coiled, tension between hips and shoulders

TRANSITION/DOWNSWING:
✓ Lower body leads FIRST — hips bump toward target then rotate
✓ Lag maintained — club drops behind trail shoulder into "the slot"
✓ Arms "fall" from the inside, not thrown from the top
✗ CASTING = releasing lag from the top, like throwing the club — kills distance, causes fat/thin
✗ OVER-THE-TOP = arms/shoulders dominate transition, club comes over the plane — causes pull/slice

IMPACT:
✓ Hands ahead of clubhead (forward shaft lean) — critical for irons
✓ Lead wrist flat/bowed, trail wrist bent back
✓ Hips open 30-45° to target
✓ 65-75% weight on lead foot
✓ Head behind ball
✓ Spine angle maintained — no "standing up" or "early extension"
✓ Eyes on ball

FOLLOW-THROUGH:
✓ Full arm extension through the ball — no chicken wing (lead elbow flying out)
✓ Forearms rotate naturally (release)
✓ Club continues on inside path after impact

FINISH:
✓ 90-95% weight on lead foot
✓ Trail foot up on toe
✓ Chest facing target (or just past)
✓ Hands high — club behind head or neck
✓ Balanced and held — not falling, not collapsing

FEEL CUES LIBRARY (use these, they work):
- "Feel like you're swinging to right field" (for over-the-top/slicers)
- "Bump your lead hip toward the target before you turn" (transition)
- "Keep the triangle your arms form through the takeaway"
- "Hold the lag like you're carrying a tray of drinks"
- "Turn your chest to the target, don't slide at it"
- "Finish with your belt buckle facing the target"
- "Feel like your lead elbow is pointing at the ground through impact"
- "Imagine pushing the butt of the club toward the ball as long as possible"
- "Squeeze the ground with your trail foot on the backswing"

RESPONSE FORMAT — return only valid JSON, no markdown, no preamble:
{
  "overallScore": number (0-100, be realistic — 60 is a decent club golfer),
  "overallRating": "Tour Ready" | "Solid Amateur" | "Good Club Golfer" | "Developing" | "Beginner",
  "handicapEstimate": string (e.g. "5-12" or "18-25" or "30+"),
  "headline": string (one punchy sentence summarizing the swing — what stands out most),
  "missExplanation": string (plain-language explanation of HOW the reported miss connects to what you see in the swing — 2-3 sentences, conversational tone),
  "phases": [
    {
      "name": string,
      "emoji": string (one relevant emoji),
      "rating": "great" | "solid" | "needs-work" | "fix-first",
      "whatISee": string (plain language, 1-2 sentences, what's literally happening),
      "whyItMatters": string (1 sentence — why this affects the shot),
      "fix": string (specific actionable fix in plain language),
      "feelCue": string (a short, memorable feel cue they can use on the range)
    }
  ],
  "topPriority": {
    "fault": string (the single most important thing to fix),
    "why": string (why this one first — 1-2 sentences),
    "drill": string (specific drill with exact steps — be concrete, not vague),
    "feelCue": string (the one thing to feel)
  },
  "strengths": string[] (2-3 genuine positives — find something real, golfers need encouragement),
  "encouragement": string (1-2 sentences of genuine, specific encouragement to close the analysis)
}"""

CHAT_SYSTEM_PROMPT = """You are SwingCamIQ, a warm and knowledgeable golf coach. You're having a follow-up conversation with a golfer about their swing analysis. Keep answers conversational, specific, and actionable. Reference details from their analysis when relevant. Be encouraging but honest. Keep responses concise — 2-4 sentences unless a drill requires more detail."""


# ── Frame extraction ───────────────────────────────────────────────────────────
def extract_frames_ffmpeg(video_path: str, num_frames: int, session_id: str) -> tuple[list[str], list[str]]:
    """Extract evenly spaced frames. Returns (base64_list, url_list)."""

    probe = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", video_path
    ], capture_output=True, text=True)

    probe_data = json.loads(probe.stdout)
    duration = None
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "video":
            duration = float(stream.get("duration", 0))
            break

    if not duration or duration <= 0:
        probe2 = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", video_path
        ], capture_output=True, text=True)
        fmt = json.loads(probe2.stdout)
        duration = float(fmt.get("format", {}).get("duration", 5))

    start = duration * 0.05
    end = duration * 0.95
    interval = (end - start) / (num_frames - 1) if num_frames > 1 else 0

    # Create session frame directory
    session_frames_dir = FRAMES_DIR / session_id
    session_frames_dir.mkdir(parents=True, exist_ok=True)

    frames_b64 = []
    frame_urls = []

    for i in range(num_frames):
        timestamp = start + interval * i
        filename = f"frame_{i:03d}.jpg"
        out_path = session_frames_dir / filename

        result = subprocess.run([
            "ffmpeg", "-ss", str(timestamp),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "3",
            "-vf", "scale=960:-2",
            str(out_path), "-y"
        ], capture_output=True)

        if out_path.exists():
            with open(str(out_path), "rb") as f:
                frames_b64.append(base64.b64encode(f.read()).decode())
            frame_urls.append(f"/frames/{session_id}/{filename}")

    return frames_b64, frame_urls


# ── Analyze endpoint ───────────────────────────────────────────────────────────
@app.post("/analyze")
async def analyze_swing(
    video: UploadFile = File(...),
    camera_angle: str = Form("face-on"),
    club_type: str = Form("iron"),
    common_miss: str = Form(""),
    golfer_name: str = Form(""),
    num_frames: int = Form(8),
    skill_level: str = Form("intermediate"),
    email: str = Form(""),
):
    if not video.content_type or not video.content_type.startswith("video/"):
        raise HTTPException(400, "File must be a video")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set in environment")

    # ── Usage gate ─────────────────────────────────────────────────────────────
    email_clean = email.strip().lower()
    conn = get_db()
    if email_clean:
        user = get_or_create_user(conn, email_clean)
        if not user_can_analyze(user):
            conn.close()
            raise HTTPException(402, "free_limit_reached")
    conn.close()

    session_id = str(uuid.uuid4())

    with tempfile.NamedTemporaryFile(suffix=Path(video.filename).suffix or ".mp4", delete=False) as tmp:
        content = await video.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        frames_b64, frame_urls = extract_frames_ffmpeg(tmp_path, min(max(num_frames, 4), 12), session_id)
        if not frames_b64:
            raise HTTPException(500, "Could not extract frames from video. Is ffmpeg installed?")

        name = golfer_name.strip() or "the golfer"
        miss_context = f"\nCommon miss/ball flight reported by {name}: {common_miss}" if common_miss.strip() else ""

        content_blocks = [
            {
                "type": "text",
                "text": f"""Analyze this golf swing for {name}.
Camera angle: {camera_angle}
Club type: {club_type}
Self-reported skill level: {skill_level}{miss_context}

I'm providing {len(frames_b64)} frames extracted at evenly-spaced intervals across the swing (chronological order). Phase labels are approximate. Analyze everything you can see and return the JSON analysis."""
            }
        ]

        phase_labels = ["Address", "Takeaway", "Halfway Back", "Top", "Transition", "Halfway Down", "Impact", "Follow-Through", "Finish", "Finish+"]
        for i, frame_b64 in enumerate(frames_b64):
            label = phase_labels[min(i, len(phase_labels) - 1)]
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}
            })
            content_blocks.append({
                "type": "text",
                "text": f"[Frame {i+1}/{len(frames_b64)} — {label}]"
            })

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}]
        )

        raw = response.content[0].text
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            raise HTTPException(500, f"Could not parse analysis response: {raw[:200]}")

        result = json.loads(json_match.group())
        result["framesExtracted"] = len(frames_b64)
        result["frameUrls"] = frame_urls
        result["sessionId"] = session_id

        # Build phase labels mapping
        phase_labels_map = []
        for i in range(len(frame_urls)):
            phase_labels_map.append(phase_labels[min(i, len(phase_labels) - 1)])
        result["frameLabels"] = phase_labels_map

        # Persist session to DB
        conn = get_db()
        conn.execute("""
            INSERT INTO sessions
              (id, created_at, golfer_name, skill_level, club_type, camera_angle,
               common_miss, overall_score, overall_rating, handicap_estimate,
               headline, full_result, frame_count, email)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_id,
            datetime.utcnow().isoformat(),
            golfer_name.strip(),
            skill_level,
            club_type,
            camera_angle,
            common_miss.strip(),
            result.get("overallScore"),
            result.get("overallRating"),
            result.get("handicapEstimate"),
            result.get("headline"),
            json.dumps(result),
            len(frames_b64),
            email.strip(),
        ))
        conn.commit()

        # Decrement free uses if on free plan
        if email_clean:
            user_row = conn.execute("SELECT * FROM users WHERE email=?", (email_clean,)).fetchone()
            if user_row and dict(user_row)["plan"] == "free":
                conn.execute(
                    "UPDATE users SET free_uses_used = free_uses_used + 1 WHERE email=?",
                    (email_clean,)
                )
                conn.commit()

        conn.close()

        # Add remaining uses to response
        if email_clean:
            conn2 = get_db()
            u = get_or_create_user(conn2, email_clean)
            conn2.close()
            result["usesRemaining"] = uses_remaining(u)
        else:
            result["usesRemaining"] = FREE_LIMIT

        return result

    except json.JSONDecodeError as e:
        raise HTTPException(500, f"JSON parse error: {str(e)}")
    except anthropic.APIError as e:
        raise HTTPException(500, f"Anthropic API error: {str(e)}")
    finally:
        os.unlink(tmp_path)


# ── Sessions endpoints ─────────────────────────────────────────────────────────
@app.get("/sessions")
def list_sessions():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, created_at, golfer_name, club_type, camera_angle,
               overall_score, overall_rating, headline, frame_count
        FROM sessions ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Session not found")
    data = dict(row)
    if data.get("full_result"):
        data["full_result"] = json.loads(data["full_result"])
    return data


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    conn = get_db()
    conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    # Clean up frames
    session_frames = FRAMES_DIR / session_id
    if session_frames.exists():
        import shutil
        shutil.rmtree(str(session_frames))
    return {"deleted": session_id}


# ── Chat endpoints ─────────────────────────────────────────────────────────────
@app.get("/sessions/{session_id}/chat")
def get_chat(session_id: str):
    conn = get_db()
    rows = conn.execute("""
        SELECT role, content, created_at FROM chat_messages
        WHERE session_id=? ORDER BY created_at ASC
    """, (session_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/sessions/{session_id}/chat")
async def post_chat(session_id: str, body: dict):
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(400, "Message required")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set")

    conn = get_db()
    session_row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session_row:
        conn.close()
        raise HTTPException(404, "Session not found")

    session = dict(session_row)
    full_result = json.loads(session.get("full_result") or "{}")

    # Load chat history
    history_rows = conn.execute("""
        SELECT role, content FROM chat_messages
        WHERE session_id=? ORDER BY created_at ASC
    """, (session_id,)).fetchall()

    messages = []
    # Inject swing context as first user/assistant exchange
    context = f"""The golfer's swing analysis summary:
- Overall score: {full_result.get('overallScore')}/100 ({full_result.get('overallRating')})
- Headline: {full_result.get('headline')}
- Top priority fix: {full_result.get('topPriority', {}).get('fault')}
- Club: {session.get('club_type')}, Skill: {session.get('skill_level')}
- Common miss: {session.get('common_miss') or 'not specified'}
"""
    messages.append({"role": "user", "content": context})
    messages.append({"role": "assistant", "content": "Got it — I have the full analysis in front of me. What would you like to know?"})

    for row in history_rows:
        messages.append({"role": row["role"], "content": row["content"]})

    messages.append({"role": "user", "content": message})

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=CHAT_SYSTEM_PROMPT,
        messages=messages,
    )
    reply = response.content[0].text

    now = datetime.utcnow().isoformat()
    conn.execute("INSERT INTO chat_messages (session_id,role,content,created_at) VALUES (?,?,?,?)",
                 (session_id, "user", message, now))
    conn.execute("INSERT INTO chat_messages (session_id,role,content,created_at) VALUES (?,?,?,?)",
                 (session_id, "assistant", reply, now))
    conn.commit()
    conn.close()

    return {"reply": reply}
