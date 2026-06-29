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

app = FastAPI(title="SwingCamIQ API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Resend email config ────────────────────────────────────────────────────────
import resend as _resend_mod
_resend_mod.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = "SwingCamIQ <coach@swingcamiq.com>"

def send_swing_report(to_email, result, golfer_name):
    if not _resend_mod.api_key or not to_email:
        return
    try:
        name = golfer_name.strip() or "Golfer"
        score = result.get("overallScore", "--")
        rating = result.get("overallRating", "--")
        handicap = result.get("handicapEstimate", "--")
        headline = result.get("headline", "")
        priority = result.get("topPriority", {})
        fault = priority.get("fault", "")
        drill = priority.get("drill", "")
        feel = priority.get("feelCue", "")
        strengths = result.get("strengths", [])
        encouragement = result.get("encouragement", "")
        strengths_html = "".join(f"<li>{s}</li>" for s in strengths)
        html = f"""<html><body style="font-family:Georgia,serif;background:#f4efe4;margin:0;padding:0">
<div style="max-width:600px;margin:0 auto;padding:32px 20px">
<div style="text-align:center;margin-bottom:28px">
  <span style="font-size:28px;font-weight:900;color:#1c3829">SwingCam<span style="color:#b04e28">IQ</span></span>
  <div style="font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#9a9a8e;margin-top:4px">Video Swing Coach</div>
</div>
<div style="background:#1c3829;border-radius:6px;padding:28px;margin-bottom:20px">
  <div style="font-style:italic;font-size:20px;color:#f4efe4;line-height:1.4;margin-bottom:20px">{headline}</div>
  <table><tr>
    <td style="background:rgba(255,255,255,.08);border-radius:4px;padding:10px 16px;text-align:center;margin-right:8px">
      <div style="font-size:28px;font-weight:900;color:#7ec492">{score}</div>
      <div style="font-size:9px;letter-spacing:2px;color:rgba(255,255,255,.4)">SCORE</div>
    </td>
    <td style="width:12px"></td>
    <td style="background:rgba(255,255,255,.08);border-radius:4px;padding:10px 16px;text-align:center">
      <div style="font-size:15px;font-weight:900;color:#7ec492;padding-top:4px">{rating}</div>
      <div style="font-size:9px;letter-spacing:2px;color:rgba(255,255,255,.4)">RATING</div>
    </td>
    <td style="width:12px"></td>
    <td style="background:rgba(255,255,255,.08);border-radius:4px;padding:10px 16px;text-align:center">
      <div style="font-size:16px;font-weight:900;color:#7ec492;padding-top:4px">{handicap}</div>
      <div style="font-size:9px;letter-spacing:2px;color:rgba(255,255,255,.4)">HDCP EST.</div>
    </td>
  </tr></table>
</div>
<div style="background:#b04e28;border-radius:6px;padding:24px;margin-bottom:20px">
  <div style="font-size:9px;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.5);margin-bottom:6px">#1 Priority Fix</div>
  <div style="font-size:18px;font-weight:900;color:#fff;margin-bottom:10px">{fault}</div>
  <div style="background:rgba(0,0,0,.18);border-radius:4px;padding:12px;margin-bottom:12px">
    <div style="font-size:9px;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.45);margin-bottom:6px">The Drill</div>
    <div style="font-size:13px;color:rgba(255,255,255,.85);line-height:1.6">{drill}</div>
  </div>
  <div style="font-style:italic;font-size:15px;color:rgba(255,255,255,.85);border-top:1px solid rgba(255,255,255,.15);padding-top:12px">"{feel}"</div>
</div>
{"<div style='background:#fff;border:1px solid #d8cebc;border-radius:6px;padding:20px;margin-bottom:20px'><div style='font-size:16px;font-weight:900;color:#1c3829;margin-bottom:12px'>What You are Doing Well</div><ul style='margin:0;padding-left:18px;color:#3d3d35;font-size:14px;line-height:1.8'>" + strengths_html + "</ul></div>" if strengths else ""}
{"<div style='background:#fdf6de;border:1px solid #e0cc88;border-radius:6px;padding:16px 20px;margin-bottom:20px;font-style:italic;font-size:15px;color:#1a1a16;line-height:1.6'>" + encouragement + "</div>" if encouragement else ""}
<div style="text-align:center;padding:24px 0;border-top:1px solid #d8cebc">
  <a href="https://swingcamiq.com" style="display:inline-block;background:#1c3829;color:#f4efe4;text-decoration:none;padding:12px 28px;border-radius:4px;font-size:14px;font-weight:700">View Full Report</a>
  <div style="margin-top:16px;font-size:11px;color:#9a9a8e">SwingCamIQ · swingcamiq.com</div>
</div>
</div></body></html>"""
        _resend_mod.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": f"Your SwingCamIQ Report - {score}/100 - {rating}",
            "html": html,
        })
    except Exception as e:
        print(f"Email send error: {e}")

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




@app.get("/admin/users")
def admin_users():
    conn = get_db()
    rows = conn.execute("""
        SELECT u.email, u.free_uses_used, u.plan, u.created_at,
               COUNT(s.id) as session_count
        FROM users u
        LEFT JOIN sessions s ON s.email = u.email
        GROUP BY u.email
        ORDER BY u.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Prompts ────────────────────────────────────────────────────────────────────
DRILL_LIBRARY = {
    "GRIP": {
        "header": "GRIP / CLUBFACE CONTROL",
        "text": """- Knuckle Check: Take your grip, look down — for a neutral grip you should see 2-2.5 knuckles on your lead hand. Too many = strong/hook-prone, too few = weak/slice-prone. Re-grip until correct, rehearse 10 reps before every range session.
- Logo Drill: Put a glove or towel logo facing the target at address; through impact, the logo should rotate to face the ground, not still face the sky (open/cupped) or face the ground too early (closed/hooded).
- Lead Wrist Flat Drill: Take the club to waist-height on the backswing and stop. Check your lead wrist in a mirror or on video — it should be flat, not cupped. Hold for 3 seconds, repeat 10x. (Driver/iron — full swing only; less relevant on short wedge shots where there's no waist-high backswing checkpoint.)""",
        "always": True, "clubs": [], "miss": [],
    },
    "SETUP": {
        "header": "SETUP / POSTURE",
        "text": """- Wall Drill (Spine Angle): Stand with your butt lightly touching a wall, hinge from the hips until your hands hang naturally — that's your spine angle. Step away and recreate it at the ball.
- Alignment Stick Across Hips: Lay a club or stick across your hip bones at address to visually confirm they're square (or intentionally open/closed) to the target line.
- Mirror Setup Check: Set up in front of a mirror, side-on. Check: spine tilt, knee flex, weight 50/50, arms hanging (not reaching). Take a photo and compare to a tour pro's setup at the same club.""",
        "always": True, "clubs": [], "miss": [],
    },
    "TAKEAWAY": {
        "header": "TAKEAWAY",
        "text": """- Headcover Under Lead Arm: Tuck a headcover or glove under your lead armpit and make slow takeaways without dropping it — forces the one-piece shoulders/arms/hands move.
- Towel Across Chest: Hold a towel across your chest, hands gripping each end on top of the grip. Make a takeaway — the towel should stay flat against your chest through the first 2 feet, confirming the body and arms move together.""",
        "always": False, "clubs": ["driver", "wood", "hybrid", "iron"], "miss": [],
    },
    "BACKSWING": {
        "header": "BACKSWING / TOP OF SWING",
        "text": """- Step-Away Drill (Weight Load): Take your normal stance, then step your trail foot back 6 inches and make backswings — exaggerates the feeling of loading into the trail side without swaying.
- Pump Drill: Swing to the top, pump halfway down and back to the top 2-3 times, then complete the swing. Builds awareness of the top position and a controlled transition. (Driver/iron — full swing.)
- Box Drill (Top of Backswing): Place an object (water bottle, headcover) just outside your trail foot at the top-of-backswing position. If the club consistently knocks it over, you're getting too far across/long at the top.""",
        "always": False, "clubs": ["driver", "wood", "hybrid", "iron"], "miss": [],
    },
    "TRANSITION": {
        "header": "TRANSITION / DOWNSWING (over-the-top, casting)",
        "text": """- Step-Through Drill: As you start the downswing, let your trail foot step toward the target (like a baseball throw). Forces the lower body to lead instead of arms/shoulders spinning first. (Driver/iron/wood — full swing only.)
- Towel Under Trail Arm: Tuck a towel under your trail armpit, make full swings without it falling before impact — keeps the trail arm connected and prevents the "throwing" motion of casting.
- Half-Swing Step Drill: Make half-speed swings focusing only on the feeling of your hips starting the downswing 0.5 seconds before your arms — say "hips, then arms" out loud as a tempo cue.
- Pause-at-Top Drill: Swing to the top and pause for a full second before starting down — eliminates the rushed, arms-first transition that causes casting and over-the-top moves.""",
        "always": False, "clubs": ["driver", "wood", "hybrid", "iron"],
        "miss": ["slice", "hook", "pull", "push", "over-the-top", "cast", "casting"],
    },
    "IMPACT": {
        "header": "IMPACT (early extension, fat/thin, low point control)",
        "text": """- Impact Bag Drill: Set an impact bag (or a pillow) where the ball would be, make slow-motion swings into it, focusing on hands ahead of the bag and a flat lead wrist at the moment of contact.
- Towel Under Both Arms (Connection): Tuck a small towel under both armpits, make 3/4 swings without dropping it — keeps the arms connected to the body through impact, helps prevent early extension and casting.
- Chair/Alignment Stick Behind Hips: Set a chair or stick just behind your trail hip at address. Make swings without your hips bumping into it on the way down — directly trains out early extension (standing up through the shot).
- Step Drill for Low Point: Place a tee or coin just ahead of the ball (target-side) for irons/wedges. Try to take the tee, not the ground behind it — trains hitting the ball, then the ground, not the reverse.
- Drop-and-Catch Tee Drill: Push a tee into the ground at the ball's location, angled toward the target. Practice swings (no ball) — you should clip the top of the tee without driving it into the ground, confirming a shallow-enough or steep-enough (club-dependent) angle of attack.""",
        "always": True, "clubs": [], "miss": [],
    },
    "FOLLOWTHROUGH": {
        "header": "FOLLOW-THROUGH / RELEASE (chicken wing, no extension)",
        "text": """- Towel Behind Lead Elbow: Place a towel or glove in the crook of your lead elbow at setup. Swing through impact — if it falls before you reach extension, you're collapsing/chicken-winging the lead arm too early.
- Reach for the Target Drill: After impact, consciously feel like you're reaching the clubhead down the target line as long as possible before letting the arms fold — exaggerate this 10x at half speed.""",
        "always": False, "clubs": ["driver", "wood", "hybrid", "iron"], "miss": [],
    },
    "FINISH": {
        "header": "FINISH / BALANCE",
        "text": """- Hold the Finish Drill: After every range swing, hold your finish position for a full 3-count before relaxing. If you can't hold it, your swing is out of balance — note which way you fall (forward = good aggressive move; backward = weight stuck on trail side).
- One Foot Up Drill: Once comfortable, try finishing with your trail foot completely off the ground, balanced only on the lead foot toe of the trail shoe — builds full weight transfer.""",
        "always": False, "clubs": ["driver", "wood", "hybrid", "iron"], "miss": [],
    },
    "SLICE": {
        "header": "SLICE (over-the-top, weak grip, open face)",
        "text": """- Gate Drill: Set two tees just outside the toe and heel of the clubhead at address, wide enough for the club to pass through cleanly on a square path. A slice path will clip the outside tee on the way back or through.
- Right Field Throw Feel: Rehearse the downswing feeling like you're throwing a ball to right field (for a RH golfer) — trains an in-to-out path instead of over-the-top.
- Split Hand Drill: Grip with hands separated by 2-3 inches, make slow swings — exaggerates the feeling of the trail hand/forearm rotating through impact, which closes an open face.""",
        "always": False, "clubs": [],
        "miss": ["slice", "fade", "pull", "open face", "over-the-top"],
    },
    "HOOK": {
        "header": "HOOK (closed face, too much in-to-out, flipping)",
        "text": """- Step Drill with Hold: Make swings holding the finish with the clubface still visibly "looking" at the sky, not the ground — trains against over-rotating/flipping the face shut.
- Weak Grip Checkpoint: Temporarily weaken the grip by half a knuckle and hit a few shots — if the hook disappears, grip strength was a contributing factor (not the only fix, but a fast diagnostic).""",
        "always": False, "clubs": [],
        "miss": ["hook", "draw", "push", "closed face"],
    },
    "FATTHIN": {
        "header": "FAT / THIN CONTACT",
        "text": """- Towel 2 Inches Behind Ball: Place a towel or line of tees 2 inches behind the ball. If you strike the towel/tees, you're hitting fat — confirms low point is too far back. (Note: for wedges, a strike just slightly behind the ball/at the ball is often correct — don't over-correct a normal descending wedge strike.)
- Coin Under Ball Drill: Place a coin directly under the ball. Try to "pick the coin clean" without the club touching the ground before it — useful for thin-contact golfers who scoop instead of compress.""",
        "always": False, "clubs": [],
        "miss": ["fat", "thin", "topped", "chunk", "chunked", "scoop"],
    },
    "SHANK": {
        "header": "SHANK",
        "text": """- Toe Up Drill: Place a second ball or tee just outside the toe of the club at address. Make swings without hitting it — directly trains the arms staying connected to the body instead of pushing out toward the ball (the #1 shank cause).
- Heel Lift Check: At setup, lift the club so only the toe touches the ground briefly, then set it back down — re-centers the connection between arms and body before swinging.""",
        "always": False, "clubs": [], "miss": ["shank", "hosel"],
    },
    "TEMPO": {
        "header": "TEMPO / TIMING",
        "text": """- 1-2-3 Count Drill: Count "1" on the takeaway, "2" at the top, "3" through impact, out loud or in your head — smooths out a rushed, lurching tempo. (Especially useful on wedges/short shots where tempo tends to get jerky from trying to control distance.)
- Metronome Drill: Use a metronome app (or just hum a steady beat) and match your backswing-to-downswing ratio to it — most efficient swings run close to a 3:1 backswing:downswing time ratio.""",
        "always": True, "clubs": [], "miss": [],
    },
    "DRIVER": {
        "header": "DRIVER",
        "text": """- Tee Height Check: Tee the ball so half the ball sits above the crown of the driver at address — wrong tee height is an underrated cause of both topped and popped-up drives.
- Upward Brush Drill: Place an alignment stick or shaft on the ground angled slightly upward toward the target, mimicking the driver's ideal upward attack angle. Practice swings brushing just under the stick.""",
        "always": False, "clubs": ["driver"], "miss": [],
    },
    "WOODHYBRID": {
        "header": "FAIRWAY WOOD / HYBRID",
        "text": """- Tee It Slightly Drill: Practice hitting off a very low tee (ball barely above the ground) to rehearse the shallower, sweeping contact these clubs want, versus an iron's steeper dig.""",
        "always": False, "clubs": ["wood", "hybrid"], "miss": [],
    },
    "IRON": {
        "header": "IRON",
        "text": """- Divot-After-Ball Drill: Draw a chalk line or use spray paint on the practice mat/turf at the ball position. Confirm your divot starts just in front of (target-side of) the line, not behind it.
- Step Drill for Compression: Set up with weight slightly favoring the lead side (55-60%) before even swinging, to rehearse the forward-leaning, compressing strike irons want at impact.""",
        "always": False, "clubs": ["iron"], "miss": [],
    },
    "WEDGE": {
        "header": "WEDGE",
        "text": """- Clock Drill: Practice swings to "9 o'clock," "10 o'clock," and "11 o'clock" backswing lengths (using the lead arm as the clock hand, ball as center) to build feel for partial-shot distance control without changing tempo.
- Bounce Awareness Drill: Open the clubface slightly at address and feel the sole (bounce) sliding along the ground through impact rather than the leading edge digging — especially useful for golfers who chunk wedge shots.
- Putt-Chip-Pitch Ladder: Hit the same shot with progressively more carry/roll (putter-like chip, then bump-and-run, then full pitch) to build a feel for matching technique to the actual shot needed, rather than one wedge swing for everything.""",
        "always": False, "clubs": ["wedge"], "miss": [],
    },
    "PUTTING": {
        "header": "PUTTING STROKE",
        "text": """- Gate Drill (Putting): Set two tees just wider than the putter head, a few inches in front of the ball. Putt through the gate without clipping either tee — trains a square, on-path stroke.
- Clock Putting Drill: Place balls at 2, 3, and 4 feet around the hole at "clock" positions and putt each in succession — builds short-putt confidence and reveals any directional bias (e.g., consistently missing left = face issue).
- Coin/Line Alignment Drill: Use the printed line on the ball (or a Sharpie line) and aim it directly at the target on every putt to confirm setup alignment before stroke mechanics even matter.
- Pendulum Drill: Putt with eyes closed, focusing only on feeling an even-tempo, equal-length backstroke and through-stroke — fixes a common fault of decelerating into the ball.""",
        "always": False, "clubs": ["putter"],
        "miss": ["putt", "putting", "three-putt", "missed putt"],
    },
    "CHIPPING": {
        "header": "CHIPPING / PITCHING",
        "text": """- Landing Spot Drill: Pick a specific landing spot (not the hole) for each chip and focus entirely on carrying the ball to that spot, letting it roll out — fixes golfers who only think about the hole and misjudge carry distance.
- Leading Edge Low-Point Drill: Make practice chip swings brushing the grass at the same low point repeatedly before hitting a ball — chipping inconsistency is most often an inconsistent low point, not a technique flaw.
- One-Hop Drill: From just off the green, practice landing the ball so it takes exactly one hop before releasing toward the hole — builds touch and trajectory control together.""",
        "always": False, "clubs": [],
        "miss": ["chip", "chipping", "pitch", "pitching", "chunk", "chili dip"],
    },
    "BUNKER": {
        "header": "BUNKER PLAY",
        "text": """- Line in the Sand Drill: Draw a line in the sand and practice hitting the line (not a ball) consistently, entering the sand 1-2 inches behind where the line is — builds the shallow, sand-displacing strike a good bunker shot needs.
- Open Face, Swing Hard Drill: Many bunker misses come from an unconsciously square/closed face and a tentative swing. Deliberately open the face more than feels natural and commit to a full swing — counterintuitively, this is often the fix for chunked or thin bunker shots.""",
        "always": False, "clubs": [], "miss": ["bunker", "sand", "trap"],
    },
}


def build_drill_library_text(club_type: str, common_miss: str) -> str:
    """
    Selects only the drill categories relevant to this analysis and returns
    them as prompt-ready text, instead of sending all 52 drills every time.
    """
    club_lower = (club_type or "").strip().lower()
    miss_lower = (common_miss or "").strip().lower()
    is_putter = club_lower == "putter"

    # GRIP and IMPACT drills assume a full swing (knuckle checks on a full
    # grip, impact bags, etc.) and don't apply to a putting stroke. SETUP
    # and TEMPO generalize reasonably and stay "always" even for putter.
    ALWAYS_EXCEPT_PUTTER = {"GRIP", "IMPACT"}

    # These categories are full-swing-only by definition, but their miss-tag
    # keywords (pull, push, fat, hook...) can ambiguously also describe a
    # missed putt ("I keep pulling putts left"). Never fire these for a
    # putter analysis regardless of miss-text match — PUTTING covers the
    # putting-specific version of these same directional misses instead.
    MISS_TRIGGERED_FULL_SWING_ONLY = {"TRANSITION", "SLICE", "HOOK", "FATTHIN", "SHANK"}

    blocks = []
    for key, entry in DRILL_LIBRARY.items():
        if is_putter and key in MISS_TRIGGERED_FULL_SWING_ONLY:
            include = False
        elif entry["always"]:
            include = not (is_putter and key in ALWAYS_EXCEPT_PUTTER)
        else:
            include = False
            if entry["clubs"]:
                include = any(tag in club_lower for tag in entry["clubs"])
            if not include and entry["miss"]:
                include = any(tag in miss_lower for tag in entry["miss"])
        if include:
            blocks.append(f"{entry['header']}\n{entry['text']}")

    return "\n\n".join(blocks)


SYSTEM_PROMPT = """You are SwingCamIQ, a warm and knowledgeable golf instructor with 20+ years of experience coaching everyday golfers of all skill levels. You've seen every fault, every miss, every frustration — and you know how to explain fixes in plain language that actually makes sense on the range.

Your job is to analyze golf swing images and give feedback that is:
- HONEST but encouraging — not harsh, not sugarcoating
- PLAIN LANGUAGE — no jargon without explanation. Say "your hips spin too early" not "premature pelvis rotation causes path deviation"
- SPECIFIC — tell them exactly what you see, not vague platitudes
- ACTIONABLE — every fault gets a feel cue AND a drill they can do today
- PRIORITIZED — focus on the 1-2 things that will make the biggest difference, not 10 things at once
- CALIBRATED — adjust both your grading standard and your focus based on the golfer's club and skill level (see below). The same visual flaw means something different on a driver than a wedge, and for a beginner than a scratch player.

READING THE FRAMES — IMPORTANT:
You are given a chronological sequence of frames spanning address to finish, evenly spaced in TIME (not in swing phase). They are NOT pre-labeled with phase names. Do not assume frame position tells you the phase — a golfer with a slow backswing and fast downswing will have "the top" appear in a different frame than one with even tempo, and this ratio itself varies by club (wedge swings often have more even tempo; driver swings often have a longer backswing-to-downswing ratio). Look at each frame and determine for yourself what position the golfer is actually in — club position, body rotation, weight distribution — rather than trusting where it falls in the sequence. Identify and name the phases based on what you observe.

CLUB-SPECIFIC CALIBRATION:
The golfer will tell you which club they hit. Correct technique differs meaningfully by club — do not grade a driver swing and a wedge swing against the same checklist. Use this as your baseline, then adjust for what's actually being asked of that club:

DRIVER:
- Ball position forward (off lead heel), wider stance, shallower attack angle — hitting UP on the ball or level is good, not a fault
- Longer, fuller backswing is expected and rewarded — more shoulder turn, more weight load
- Higher hand position and more "extension" through impact; finish is tall and full
- Slight upward angle of attack is a strength here, not a fat-shot warning sign — do not penalize it the way you would with an iron

FAIRWAY WOOD / HYBRID:
- Similar principles to driver but slightly more forward ball-strike feel — ball position just forward of center, attack angle closer to neutral/level
- Still rewards a fuller turn, but less tolerance for an overly steep, iron-like descending blow

IRON:
- Ball position center to slightly forward depending on iron length
- Neutral to slightly descending attack angle is correct — a divot AFTER the ball is good, not a fault
- Forward shaft lean at impact is important and should be checked for
- Standard full-swing mechanics checklist (turn, lag, lower-body lead) applies most directly here

WEDGE:
- Shorter, more compact swing is often correct, NOT a flaw — don't penalize a shorter backswing on a wedge shot the way you would on a driver
- More descending, steeper attack angle is correct and desired — a deeper divot is normal and good here, not "casting" or "hitting fat"
- Often less full hip/shoulder turn than a full swing — that's appropriate for the shorter shot, not "restricted rotation"
- Weight distribution may stay more centered/lead-side biased throughout, rather than the big weight shift you'd want on a driver
- Tempo is often smoother and more even than a full swing — don't expect or require a long, loaded backswing

PUTTER:
- This is a completely different motion from a full swing. Do NOT apply the SWING PHASES checklist below — use the PUTTING STROKE checklist instead.
- There is no backswing/downswing in the full-swing sense, no weight transfer, no hip or shoulder turn driving the motion, and no "top of backswing." The stroke is a short pendulum motion from the shoulders and arms.
- The lower body and head should stay essentially still throughout — stillness is a strength here, not "restricted rotation" or a fault.
- Tempo should be smooth and symmetrical: the backstroke and through-stroke should take roughly the same amount of time, with no jab, hit, or deceleration into the ball.
- handicapEstimate is not meaningfully derivable from a putting stroke alone — give a loose/general estimate if you must, or note that it can't be determined from putting alone.

When you call out a fault, make sure it is actually a fault for THIS club, not just a deviation from full-swing-driver mechanics.

SKILL-LEVEL CALIBRATION:
The golfer will tell you their self-reported skill level: beginner, intermediate, or advanced. Use this to set your grading curve, what you prioritize, and your tone — NOT to change what you actually see in the frames.

BEGINNER:
- Grade on a curve. A beginner doing the fundamentals reasonably (balance, getting the club back and through, making contact) deserves a solid-feeling score even if technique isn't textbook — overallScore and overallRating should reflect realistic progress for someone new to the game, not be measured against a scratch-golfer standard.
- Focus almost entirely on ONE foundational issue that will unlock the most improvement — grip, setup, balance, or a single big swing fault. Ignore minor technical nuances (wrist angles, subtle face control, swing plane details) entirely; they will be noise at this stage.
- Tone should be especially encouraging. Assume they are easily discouraged by a wall of criticism — find genuine strengths to highlight.

INTERMEDIATE:
- Grade against a realistic "good club golfer" standard, not tour mechanics. Solid fundamentals with occasional breakdowns under speed or pressure is expected.
- Focus on 1-2 specific, fixable faults — can include some technical detail (e.g. early extension, casting) but always tied to a clear feel cue and drill.
- Tone: honest and direct, encouraging but treat them as someone who can handle real feedback and wants to actually improve, not just feel good.

ADVANCED:
- Grade against a tight, high standard — small deviations matter at this level and should be named precisely.
- Focus can include finer technical detail: face control through impact, lag retention, sequencing timing, consistency of low point. Don't hold back on nuance.
- Tone: direct and technical, golfer-to-golfer. Less hand-holding, more precision. Still find genuine strengths, but don't pad the praise.

BALL FLIGHT LAWS (use these to connect miss patterns to swing causes):
- SLICE (curves right for RH golfer): open clubface relative to swing path. Caused by: over-the-top path, weak grip, cupped lead wrist, casting.
- HOOK (curves hard left): closed face relative to path. Caused by: too strong grip, flipping/rolling forearms, excessive in-to-out path.
- PULL (goes straight left): face square but path is out-to-in. Often paired with slice.
- PUSH (goes straight right): face square but path is in-to-out too much.
- FADE (gentle left-to-right): slightly open face to path — can be intentional.
- DRAW (gentle right-to-left): slightly closed face to path — often a power shape.
- FAT (hits ground before ball): weight hanging back, ball too far forward, early extension, scooping. NOTE: a deeper, more deliberate strike before the ball is NORMAL and correct on a wedge — only call this a fault if it's clearly excessive or the golfer reported it as their miss.
- THIN (hits top of ball): standing up through impact, head lifting, trying to "help" ball airborne.
- TOPPED: extreme version of thin — head lifting, spine extending upward.
- SHANK (hosel strike): arms too far from body at impact, early extension pushing hands toward ball.

SWING PHASES — what to look for (adjust expectations per the CLUB-SPECIFIC CALIBRATION above):

ADDRESS/SETUP:
✔ Feet shoulder-width apart (wider for driver, narrower for wedge)
✔ Spine tilted 20-30° from waist (athletic posture, not hunched or too upright)
✔ Knees slightly flexed (not squatting)
✔ Weight 50/50, ball position varies by club (see calibration above)
✔ Arms hanging naturally, not reaching or cramped
✔ Club face square to target

TAKEAWAY (club to waist):
✔ One-piece move: shoulders, arms, hands move together
✔ Club face angle mirrors spine angle at waist height (toe pointing up)
✔ No rolling/fanning the face open
✔ No picking the club up steeply with hands

BACKSWING:
✔ Shoulder turn 80-100° for men on a full swing (less on a wedge — see calibration)
✔ Hip turn ~45° (creating "X-factor" coil) on a full swing
✔ Lead arm relatively straight (not rigid)
✔ Weight loading to trail foot (60-70% at top on a full swing; less on a wedge)
✔ Spine angle maintained — no sway (lateral slide) or dip

TOP OF BACKSWING (full swings — may be abbreviated or absent on short wedge shots):
✔ Club shaft roughly parallel to ground (slightly short is fine; expect shorter on wedge)
✔ Lead wrist flat or bowed — NOT cupped (cupped = open face = slice)
✔ Club face parallel to lead forearm
✔ Fully coiled (full swing) — tension between hips and shoulders

TRANSITION/DOWNSWING:
✔ Lower body leads FIRST — hips bump toward target then rotate
✔ Lag maintained — club drops behind trail shoulder into "the slot"
✔ Arms "fall" from the inside, not thrown from the top
✖ CASTING = releasing lag from the top, like throwing the club — kills distance, causes fat/thin
✖ OVER-THE-TOP = arms/shoulders dominate transition, club comes over the plane — causes pull/slice

IMPACT:
✔ Hands ahead of clubhead (forward shaft lean) — critical for irons and wedges, expected on driver too but less pronounced
✔ Lead wrist flat/bowed, trail wrist bent back
✔ Hips open 30-45° to target (full swing)
✔ 65-75% weight on lead foot (full swing; wedges may stay more centered)
✔ Head behind ball
✔ Spine angle maintained — no "standing up" or "early extension"
✔ Eyes on ball
✔ Attack angle should match the club — see CLUB-SPECIFIC CALIBRATION (descending for irons/wedges, neutral-to-ascending for driver)

FOLLOW-THROUGH:
✔ Full arm extension through the ball — no chicken wing (lead elbow flying out)
✔ Forearms rotate naturally (release)
✔ Club continues on inside path after impact

FINISH:
✔ 90-95% weight on lead foot (full swing — wedge finishes may be shorter/more controlled)
✔ Trail foot up on toe (full swing)
✔ Chest facing target (or just past)
✔ Hands high — club behind head or neck (full swing; shorter on partial wedge shots)
✔ Balanced and held — not falling, not collapsing

PUTTING STROKE — use THIS checklist instead of SWING PHASES above when club is putter:

SETUP:
✔ Eyes directly over or just inside the ball
✔ Shoulders square to the target line, putter face square at address
✔ Arms hang relaxed, forming a soft triangle with the shoulders
✔ Weight even, body still — no excess tension

BACKSTROKE:
✔ Motion comes from the shoulders/arms as a single pendulum unit — NOT wrists flicking independently
✔ Putter stays low to the ground, no steep pick-up
✔ Length is proportional to the putt distance, not a fixed length regardless of putt
✔ Lower body and head stay still — no sway or shift

IMPACT (putting):
✔ Putter face square to the target line at contact
✔ Slight forward press or neutral — no scooping or flipping the wrists through impact
✔ Steady head and eyes — no looking up early to see the result
✔ Smooth acceleration through the ball, not a jab or deceleration

FOLLOW-THROUGH (putting):
✔ Follow-through roughly mirrors the backstroke length — symmetrical pendulum, not a short hit with a long finish or vice versa
✔ Putter face stays square through the stroke, not rotating open or closed
✔ Body stays still until well after contact

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
- "Let the putter swing like a pendulum — same length back as through" (putting tempo)
- "Rock your shoulders, keep everything else quiet" (putting stillness)

RESPONSE FORMAT — return only valid JSON, no markdown, no preamble:
{
  "overallScore": number (0-100, curved to the golfer's self-reported skill level — a beginner doing the fundamentals reasonably should score in a solid range for a beginner, not be judged against a scratch-golfer standard. An advanced player should be held to a tighter, higher bar.),
  "overallRating": "Tour Ready" | "Solid Amateur" | "Good Club Golfer" | "Developing" | "Beginner",
  "handicapEstimate": string (e.g. "5-12" or "18-25" or "30+" — your honest estimate of their handicap based on what you see, independent of the curved score),
  "headline": string (one punchy sentence summarizing the swing — what stands out most),
  "missExplanation": string (plain-language explanation of HOW the reported miss connects to what you see in the swing — 2-3 sentences, conversational tone),
  "phases": [
    {
      "name": string (name the phase based on what you actually observe in this frame, not a fixed position-based label),
      "emoji": string (one relevant emoji),
      "rating": "great" | "solid" | "needs-work" | "fix-first",
      "whatISee": string (plain language, 1-2 sentences, what's literally happening),
      "whyItMatters": string (1 sentence — why this affects the shot),
      "fix": string (specific actionable fix in plain language),
      "feelCue": string (a short, memorable feel cue they can use on the range)
    }
  ],
  "topPriority": {
    "fault": string (the single most important thing to fix, prioritized per the SKILL-LEVEL CALIBRATION above),
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
@app.get("/admin/sessions-raw")
def admin_sessions_raw(limit: int = 20):
    conn = get_db()
    rows = conn.execute("""
        SELECT id, created_at, email, golfer_name, overall_score
        FROM sessions
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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

I'm providing {len(frames_b64)} frames extracted at evenly-spaced TIME intervals across the swing (chronological order, address to finish). These are NOT labeled by swing phase — figure out what phase each frame shows from what you actually see (club position, body rotation, weight distribution), not from its position in the sequence. Tempo varies by golfer and by club, so don't assume frame N is always "the top" or "impact."

Relevant drills for this club and miss pattern (use these — don't invent different ones unless none of these fit):
{build_drill_library_text(club_type, common_miss)}

Analyze everything you can see and return the JSON analysis."""
            }
        ]

        for i, frame_b64 in enumerate(frames_b64):
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}
            })
            content_blocks.append({
                "type": "text",
                "text": f"[Frame {i+1}/{len(frames_b64)}]"
            })

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4000,
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
        phase_labels_map = [f"Frame {i+1}" for i in range(len(frame_urls))]
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

        # Send email report
        if email_clean:
            try:
                send_swing_report(email_clean, result, golfer_name)
            except Exception as e:
                print(f"Email error: {e}")

        return result

    except json.JSONDecodeError as e:
        raise HTTPException(500, f"JSON parse error: {str(e)}")
    except anthropic.APIError as e:
        raise HTTPException(500, f"Anthropic API error: {str(e)}")
    finally:
        os.unlink(tmp_path)


# ── Sessions endpoints ─────────────────────────────────────────────────────────
@app.get("/sessions")
def list_sessions(email: str = ""):
    conn = get_db()
    if email.strip():
        rows = conn.execute("""
            SELECT id, created_at, golfer_name, club_type, camera_angle,
                   overall_score, overall_rating, headline, frame_count
            FROM sessions WHERE email=? ORDER BY created_at DESC
        """, (email.strip().lower(),)).fetchall()
    else:
        rows = []
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
