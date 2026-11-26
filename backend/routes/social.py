"""Social Post Composer — FastAPI backend.

Endpoints consumed by the social-composer.html frontend.
Covers the three external integrations:
  - OpenRouter (draft generation)
  - litterbox (image hosting, 72h expiry)
  - Buffer via mcporter (draft creation, never publish)

No secrets reach the browser. No subagent delegation.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Request

from backend.auth import require_admin
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)
router = APIRouter(tags=["social"])




# The composer spends OpenRouter credit and posts to Buffer — every write route is require_admin.

# ── Config ────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = "openai/gpt-5.6-luna"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MCPORTER_CONFIG = Path.home() / ".config" / "mcporter" / "config.json"

SKILL_FILE = Path.home() / ".hermes" / "skills" / "productivity" / "buffer-social-post" / "SKILL.md"


def _load_voice_rules() -> str:
    """Load the full voice rules from the buffer-social-post skill file.

    Falls back to a condensed set if the skill file is missing.
    The skill is the single source of truth — updates there propagate here.
    """
    try:
        text = SKILL_FILE.read_text()
        # Strip YAML frontmatter (--- ... ---)
        text = re.sub(r"^---.*?---\s*", "", text, count=1, flags=re.DOTALL).strip()
        return text
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Skill file not found at %s: %s", SKILL_FILE, exc)
        return """# Harrison voice
- Simplifies the complex. States the point plainly.
- Dry, self-deprecating, lightly humorous.
- No enthusiasm performance. No thought-leader posture.

# Blacksheep voice
- Dry, technical, numbers-forward. Proof over promise. No fluff.

# Platform rules
- LinkedIn: 2-5 short paragraphs, Unicode bold for headlines, plain URL.
- X/Twitter: ≤ 280 chars, condensed to core message, plain URL.

# Anti-slop
- NO em dashes, negative parallelism, short fragments, anaphora.
- NO banned words: quietly, delve, tapestry, ecosystem, leverage.
- NO what matters / the difference is / imagine a world where.
- NO markdown ** on LinkedIn — use Unicode bold.
- URL must be in plain text.
- Use proper newlines, not escaped \\\\n."""


VOICE_RULES = _load_voice_rules()


# ── Models ────────────────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    topic: str
    account: str  # "harrison" | "blacksheep"
    platform: str  # "linkedin" | "x"

    @field_validator("account")
    @classmethod
    def valid_account(cls, v: str) -> str:
        if v not in ("harrison", "blacksheep"):
            raise ValueError("account must be 'harrison' or 'blacksheep'")
        return v

    @field_validator("platform")
    @classmethod
    def valid_platform(cls, v: str) -> str:
        if v not in ("linkedin", "x"):
            raise ValueError("platform must be 'linkedin' or 'x'")
        return v


class CondenseRequest(BaseModel):
    text: str
    account: str  # "harrison" | "blacksheep"

    @field_validator("account")
    @classmethod
    def valid_account(cls, v: str) -> str:
        if v not in ("harrison", "blacksheep"):
            raise ValueError("account must be 'harrison' or 'blacksheep'")
        return v


class PushRequest(BaseModel):
    text: str
    channel_id: str
    image_data_url: str | None = None
    saveToDraft: bool = True

    @field_validator("saveToDraft")
    @classmethod
    def drafts_only(cls, v: bool) -> bool:
        if not v:
            raise ValueError("saveToDraft must be true — no publish path exists")
        return v


# ── Helpers ────────────────────────────────────────────────────────────

def _build_prompt(topic: str, account: str, platform: str) -> str:
    """Build the system + user prompt for OpenRouter."""
    voice = "harrison" if account == "harrison" else "blacksheep"
    plat = "LinkedIn" if platform == "linkedin" else "X/Twitter"
    return f"""{VOICE_RULES}

Write a {plat} post for the {voice} account about the following topic.
Return ONLY the post text, no preamble, no commentary, no markdown fences.
Use plain ASCII text only, no Unicode bold/italic or fancy characters.

Topic: {topic}"""


def _call_openrouter(prompt: str, max_tokens: int = 600) -> str:
    """Call the OpenRouter API and return the generated text."""
    if not OPENROUTER_API_KEY:
        raise HTTPException(503, "OpenRouter API key not configured")

    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        logger.error("OpenRouter %s: %s", resp.status_code, resp.text[:500])
        raise HTTPException(502, f"OpenRouter error: {resp.status_code}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()

    # Belt-and-braces: strip em dashes the model may have emitted
    text = text.replace("—", ", ").replace("–", "-")
    # Strip markdown bold markers
    text = text.replace("**", "")
    # Strip markdown code fences if present
    text = re.sub(r"^```.*?\n|```$", "", text, flags=re.MULTILINE).strip()

    return text


def _upload_to_litterbox(image_data_url: str) -> str:
    """Upload a base64 PNG to litterbox (72h expiry). Returns the URL."""
    try:
        raw = base64.b64decode(image_data_url.split(",", 1)[1])
    except (IndexError, ValueError) as e:
        raise HTTPException(400, f"Invalid image data URL: {e}")

    try:
        from PIL import Image as PILImage
        import io as _io
        img = PILImage.open(_io.BytesIO(raw))
        max_dim = 1600
        if max(img.width, img.height) > max_dim:
            ratio = max_dim / max(img.width, img.height)
            img = img.resize((int(img.width*ratio), int(img.height*ratio)), PILImage.LANCZOS)
        buf = _io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
        payload = buf.getvalue()
    except ImportError:
        payload = raw

    suffix = ".jpg"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(payload)
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": ("card.jpg", f, "image/jpeg")},
                timeout=30,
            )
        os.unlink(tmp_path)
        if resp.status_code != 200:
            raise HTTPException(502, f"litterbox error: {resp.status_code}")
        url = resp.text.strip()
        if not url.startswith("https://"):
            raise HTTPException(502, f"litterbox returned unexpected response: {url}")
        return url
    except requests.RequestException as e:
        os.unlink(tmp_path)
        raise HTTPException(502, f"litterbox upload failed: {e}")


def _call_buffer_create_post(text: str, channel_id: str, media_url: str | None = None) -> str:
    """Call Buffer's create_post via mcporter. Returns the post ID."""
    if not MCPORTER_CONFIG.exists():
        raise HTTPException(503, "mcporter config not found")

    with open(MCPORTER_CONFIG) as f:
        cfg = json.load(f)

    token = (
        cfg.get("mcpServers", {})
        .get("buffer", {})
        .get("env", {})
        .get("BUFFER_API_TOKEN")
    )
    if not token:
        raise HTTPException(503, "BUFFER_API_TOKEN not found in mcporter config")

    assets = []
    if media_url:
        assets.append({"image": {"url": media_url}})

    # Build the mcporter call
    env = {**os.environ, "BUFFER_API_TOKEN": token}
    args = [
        "npx", "mcporter", "call", "buffer.create_post",
        f"channelId={channel_id}",
        "schedulingType=automatic",
        "mode=addToQueue",
        f"text={text}",
        "saveToDraft=true",
    ]
    if assets:
        args.append(f"assets={json.dumps(assets)}")

    try:
        result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Buffer API timed out")
    except FileNotFoundError:
        raise HTTPException(503, "mcporter (npx) not found")

    if result.returncode != 0:
        logger.error("Buffer mcporter error: %s", result.stderr[:500])
        raise HTTPException(502, f"Buffer API error: {result.stderr[:200]}")

    # Try to extract post ID from the JSON response
    try:
        resp_data = json.loads(result.stdout)
        # Check for MCP-level errors in the response
        if isinstance(resp_data, dict) and resp_data.get("error"):
            err_msg = str(resp_data["error"])[:200]
            logger.error("Buffer API error in response: %s", err_msg)
            raise HTTPException(502, f"Buffer API error: {err_msg}")
        if isinstance(resp_data, dict):
            post_id = resp_data.get("id", resp_data.get("_id", "unknown"))
        else:
            post_id = str(resp_data)
        if post_id.startswith("MCP error") or "error" in str(post_id).lower():
            raise HTTPException(502, f"Buffer API error: {post_id}")
        return post_id
    except (json.JSONDecodeError, TypeError):
        # Fallback: return the raw response
        return result.stdout.strip()[:100]


# ── Endpoints ──────────────────────────────────────────────────────────

@router.post("/api/social/generate")
def generate(req: GenerateRequest, request: Request):
    """Generate a draft post for one account and platform."""
    require_admin(request)
    prompt = _build_prompt(req.topic, req.account, req.platform)
    text = _call_openrouter(prompt)
    return {"draft": text}


@router.post("/api/social/condense")
def condense(req: CondenseRequest, request: Request):
    """Condense a LinkedIn draft to an X/Twitter draft (≤280 chars)."""
    require_admin(request)
    prompt = f"""{VOICE_RULES}

Condense the following LinkedIn post to an X/Twitter post.
- Must be ≤ 280 characters.
- Keep the core message. Strip setup, context, and CTA.
- Keep Unicode formatting if present.
- URL in plain text.
- Return ONLY the post text, no preamble.

LinkedIn post:
{req.text}"""

    text = _call_openrouter(prompt, max_tokens=200)

    # Enforce char limit
    if len(text) > 280:
        text = text[:277] + "..."

    return {"draft": text}


@router.post("/api/social/push")
def push(req: PushRequest, request: Request):
    """Create a Buffer draft on one channel, optionally with an image."""
    require_admin(request)
    media_url = None
    if req.image_data_url:
        media_url = _upload_to_litterbox(req.image_data_url)
        logger.info("Uploaded card image to %s", media_url)

    post_id = _call_buffer_create_post(req.text, req.channel_id, media_url)
    logger.info("Buffer draft created: %s (channel %s)", post_id, req.channel_id)

    return {"status": "drafted", "post_id": post_id}