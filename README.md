# SwingIQ — AI Golf Swing Analyzer

Your personal AI golf coach. Upload a swing video, describe your miss, and get honest, plain-language coaching backed by real biomechanics.

---

## What It Does

- Accepts any golf swing video (MP4, MOV, AVI)
- Extracts 6–10 key frames using FFmpeg
- Sends frames to Claude with a detailed golf mechanics system prompt
- Returns a full coaching report:
  - Overall score + handicap estimate
  - Phase-by-phase analysis (setup → finish)
  - **Your miss explained** — connects your slice/hook/fat shot to what's happening in your swing
  - #1 priority fix with a specific drill and feel cue
  - What you're doing well
  - Encouragement to close

---

## Setup (5 minutes)

### 1. Install FFmpeg

**Mac:**
```bash
brew install ffmpeg
```

**Ubuntu/Debian:**
```bash
sudo apt install ffmpeg
```

**Windows:** Download from https://ffmpeg.org/download.html and add to PATH.

---

### 2. Set Your Anthropic API Key

Get your key from https://console.anthropic.com

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

To make it permanent, add that line to your `~/.zshrc` or `~/.bashrc`.

---

### 3. Install Python Dependencies

```bash
cd swingiq
pip install -r requirements.txt
```

Or use the startup script (does this automatically):
```bash
chmod +x start.sh
./start.sh
```

---

### 4. Run the Server

```bash
./start.sh
```

Or manually:
```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

---

### 5. Open the App

Go to **http://localhost:8000** in your browser.

---

## Filming Tips for Best Results

- **Face-on angle** (camera facing you from in front) is best for seeing weight shift, spine angle, and impact position
- **Down-the-line** (camera behind you) is best for seeing swing plane and path
- Film from **waist height**, not too low or too high
- Make sure the **full swing fits in frame** — don't crop the club
- Good lighting helps — outdoors is ideal
- Slow-motion mode on your phone (240fps) produces much better frame captures

---

## Project Structure

```
swingiq/
├── server.py          # FastAPI backend + Claude integration
├── static/
│   └── index.html     # Frontend UI
├── requirements.txt   # Python dependencies
├── start.sh           # Startup script
└── README.md          # This file
```

---

## Tech Stack

- **Backend:** Python + FastAPI
- **Frame extraction:** FFmpeg (via subprocess)
- **AI:** Anthropic Claude (claude-opus-4-5)
- **Frontend:** Vanilla HTML/CSS/JS (no build step needed)

---

## Troubleshooting

**"Failed to fetch" / connection refused**
→ Make sure the server is running (`./start.sh`)

**"ffmpeg not found"**
→ Install ffmpeg (see Step 1 above)

**"ANTHROPIC_API_KEY not set"**
→ Run `export ANTHROPIC_API_KEY=your_key` in the same terminal

**"Could not extract frames"**
→ Try a different video format. MP4 works most reliably.

**Analysis seems off / wrong phase labels**
→ The phase labels (Address, Takeaway, etc.) are approximate. Claude analyzes what it actually sees in each frame, not the label. For best results, trim your video to just the swing itself.
