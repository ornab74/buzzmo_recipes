from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable, Iterable, Optional

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception as exc:  # pragma: no cover - optional runtime dependency
    AESGCM = None
    InvalidTag = ValueError
    CRYPTO_IMPORT_ERROR = exc
else:
    CRYPTO_IMPORT_ERROR = None


APP_NAME = "Buzzmo SuperAgent Recipes"
APP_ID = "buzzmo_recipes"
DEFAULT_STREAM_URL = "https://www.twitch.tv/buzzmoo_au"
DEFAULT_QUALITY = "audio_only"
DEFAULT_VIDEO_QUALITY = "360p"
DEFAULT_CHUNK_SECONDS = 35
DEFAULT_BUCKET_EVERY = 3
MAX_TRANSCRIPT_WINDOW_CHARS = 12000
MAX_RECIPES = 300

MODEL_REPO = "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/"
MODEL_FILE = "gemma-4-E2B-it.litertlm"
MODEL_SHA256 = "ab7838cdfc8f77e54d8ca45eadceb20452d9f01e4bfade03e5dce27911b27e42"

SALT_BYTES = 16
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
VERIFY_TEXT = b"buzzmo-superagent-vault-v1"
AES_MAGIC = b"BSA1"
HMAC_STREAM_MAGIC = b"BSX1"
MODEL_CHUNK_MAGIC = b"BSM1"
MODEL_CHUNK_SIZE = 4 * 1024 * 1024
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

BUCKET_KINDS = (
    "recipe_candidate",
    "ingredient",
    "technique",
    "timing",
    "equipment",
    "substitution",
    "dietary",
    "shopping",
    "serving",
    "discard",
)

RECIPE_SYSTEM_PROMPT = """
You are SuperAgentLLM, an on-device culinary intelligence system watching a live Twitch cooking stream.
Your job is not to summarize vibes; your job is to recover usable kitchen knowledge from imperfect audio.

Operating rules:
- Treat transcript text as evidence. Do not invent exact quantities, times, temperatures, brands, or dietary claims.
- Preserve uncertainty explicitly with phrases such as "likely", "unclear", or "[uncertain: ...]".
- Prefer recipe structure that a cook can execute: components, mise en place, heat levels, timing, visual cues, and recovery notes.
- Merge repeated fragments into one coherent procedure, but keep contradictions visible in "Stream Notes".
- Keep the host's recipe intent intact. Do not optimize it into a different dish unless the transcript clearly changes direction.
- Output polished Markdown only when asked for a recipe.
""".strip()

BUCKET_SYSTEM_PROMPT = """
You are a streaming context architect. Your task is to turn noisy cooking transcript windows into durable,
mergeable buckets that can survive a long live stream and later be stitched into recipes.

Bucket discipline:
- Emit compact JSON only.
- Every bucket must be evidence-backed by source_segment_ids.
- Use low confidence when wording is ambiguous, off-topic, duplicated, or likely chat banter.
- Use stitch_key to join the same dish or component across time. Keep keys lowercase-kebab-case.
- Put raw facts in details arrays: ingredients, quantities, timings, temperatures, tools, substitutions, visual_cues, warnings, unknowns.
- Mark off-topic or low-signal fragments as kind="discard" so they do not pollute recipe context.

Return a top-level object with exactly this shape:
{"buckets":[{"kind":"","title":"","summary":"","confidence":0.0,"stitch_key":"","source_segment_ids":[],"details":{}}]}
""".strip()

TRANSCRIBE_SYSTEM_PROMPT = """
You are a culinary audio transcription specialist. Preserve spoken wording faithfully, especially ingredient
names, measurements, cooking temperatures, timings, brand names, slang, and corrections.

Rules:
- Return plain transcript text only.
- Do not summarize.
- Use punctuation and speaker-flow cleanup only when it improves readability without changing meaning.
- Mark uncertain words as [uncertain: ...].
- Keep repeated corrections when they affect a recipe, for example "not butter, ghee".
""".strip()


def default_app_dir() -> Path:
    override = os.environ.get("BUZZMOO_RECIPES_HOME") or os.environ.get("BUZZMO_RECIPES_HOME")
    if override:
        return Path(override).expanduser()
    workspace_local = Path(__file__).resolve().parent / ".superagent_data"
    if not sys.platform.startswith("win") and sys.platform != "darwin":
        return workspace_local
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base).expanduser() / APP_ID if base else Path.home() / "AppData" / "Local" / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg).expanduser() / APP_ID if xdg else Path.home() / ".local" / "share" / APP_ID


APP_DIR = default_app_dir()
DB_PATH = APP_DIR / "superagent_recipes.sqlite3"
MODEL_DIR = APP_DIR / "models"
MODEL_PATH = MODEL_DIR / MODEL_FILE
ENCRYPTED_MODEL_PATH = MODEL_DIR / f"{MODEL_FILE}.vault"
CHUNK_DIR = APP_DIR / "chunks"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any, *, max_chars: int = 4000, keep_newlines: bool = False) -> str:
    text = html.unescape(str(value or ""))
    text = CONTROL_CHARS_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not keep_newlines:
        text = re.sub(r"\s+", " ", text)
    else:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def estimate_tokens(value: Any) -> int:
    text = str(value or "")
    if not text:
        return 0
    return max(1, int(len(text) / 4.0))


def channel_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(clean_text(url, max_chars=300))
    if parsed.netloc and "twitch.tv" in parsed.netloc.lower():
        channel = parsed.path.strip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_]{3,25}", channel or ""):
            return channel.lower()
    return "unknown"


def is_twitch_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(clean_text(url, max_chars=400))
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower().endswith("twitch.tv") and channel_from_url(url) != "unknown"


def stable_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or f"recipe-{int(time.time())}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(0, value))
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"


def set_private_permissions(path: Path, *, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except Exception:
        pass


def ensure_dirs() -> None:
    for directory in (APP_DIR, MODEL_DIR, CHUNK_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        set_private_permissions(directory, directory=True)


def derive_key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("Enter a vault password.")
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=256 * 1024 * 1024,
    )


def split_key(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


def xor_stream(data: bytes, key: bytes, nonce: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out))


class CryptoBox:
    """Small encrypted envelope used for SQLite payloads.

    AES-GCM is used when cryptography is installed. The stdlib fallback is an
    HMAC-authenticated stream envelope so the app can still run in bare Python.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Vault key must be 32 bytes.")
        self.key = key

    @property
    def mode_name(self) -> str:
        return "AES-GCM" if AESGCM is not None else "HMAC stream fallback"

    def encrypt(self, raw: bytes, aad: bytes) -> bytes:
        if AESGCM is not None:
            nonce = os.urandom(12)
            return AES_MAGIC + nonce + AESGCM(self.key).encrypt(nonce, raw, aad)
        nonce = os.urandom(16)
        enc_key = split_key(self.key, b"enc")
        mac_key = split_key(self.key, b"mac")
        ciphertext = xor_stream(raw, enc_key, nonce)
        tag = hmac.new(mac_key, HMAC_STREAM_MAGIC + nonce + aad + ciphertext, hashlib.sha256).digest()
        return HMAC_STREAM_MAGIC + nonce + ciphertext + tag

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if blob.startswith(AES_MAGIC):
            if AESGCM is None:
                raise ValueError("This vault uses AES-GCM, but cryptography is not installed.")
            nonce = blob[4:16]
            payload = blob[16:]
            try:
                return AESGCM(self.key).decrypt(nonce, payload, aad)
            except InvalidTag as exc:
                raise ValueError("Encrypted payload authentication failed.") from exc
        if blob.startswith(HMAC_STREAM_MAGIC):
            nonce = blob[4:20]
            tag = blob[-32:]
            ciphertext = blob[20:-32]
            mac_key = split_key(self.key, b"mac")
            expected = hmac.new(mac_key, HMAC_STREAM_MAGIC + nonce + aad + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise ValueError("Encrypted payload authentication failed.")
            return xor_stream(ciphertext, split_key(self.key, b"enc"), nonce)
        raise ValueError("Encrypted payload has an unknown envelope.")


@dataclass
class TranscriptSegment:
    id: int
    channel: str
    started_at: str
    duration_seconds: float
    transcript: str
    refined_transcript: str
    audio_sha256: str
    status: str


@dataclass
class BucketRecord:
    id: int
    kind: str
    title: str
    summary: str
    confidence: float
    stitch_key: str
    source_segment_ids: list[int]
    created_at: str


@dataclass
class RecipeRecord:
    id: int
    title: str
    slug: str
    markdown: str
    source_bucket_ids: list[int]
    created_at: str
    updated_at: str


class EncryptedRecipeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: sqlite3.Connection | None = None
        self.box: CryptoBox | None = None
        self.lock = threading.RLock()

    @property
    def is_new(self) -> bool:
        return not self.path.exists()

    @property
    def crypto_mode(self) -> str:
        return self.box.mode_name if self.box else "locked"

    def unlock(self, password: str) -> None:
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(self.path.parent, directory=True)
        with self.lock:
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self._ensure_schema()
            salt_value = self._get_meta("salt")
            if not salt_value:
                salt = os.urandom(SALT_BYTES)
                self.box = CryptoBox(derive_key(password, salt))
                self._set_meta("salt", base64.urlsafe_b64encode(salt).decode("ascii"))
                self._set_meta("kdf", f"hashlib.scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}")
                self._set_meta("verifier", base64.b64encode(self.box.encrypt(VERIFY_TEXT, b"meta:verifier")).decode("ascii"))
                self._set_meta("created_at", utc_now())
                self.conn.commit()
                set_private_permissions(self.path)
                return
            try:
                salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
                self.box = CryptoBox(derive_key(password, salt))
                verifier = base64.b64decode((self._get_meta("verifier") or "").encode("ascii"))
                if self.box.decrypt(verifier, b"meta:verifier") != VERIFY_TEXT:
                    raise ValueError
            except (ValueError, TypeError, binascii.Error) as exc:
                self.close()
                raise ValueError("That password could not unlock this encrypted recipe vault.") from exc

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                self.conn.close()
            self.conn = None
            self.box = None

    def require_open(self) -> tuple[sqlite3.Connection, CryptoBox]:
        if self.conn is None or self.box is None:
            raise RuntimeError("Recipe vault is locked.")
        return self.conn, self.box

    def _ensure_schema(self) -> None:
        assert self.conn is not None
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buckets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                stitch_key TEXT NOT NULL,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_channel_created ON transcript_segments(channel, created_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_buckets_kind_created ON buckets(kind, created_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_recipes_updated ON recipes(updated_at DESC)")
        self.conn.commit()

    def _get_meta(self, key: str) -> str | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def encrypt_json(self, payload: dict[str, Any], aad: bytes = b"payload:v1") -> bytes:
        _conn, box = self.require_open()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return box.encrypt(raw, aad)

    def decrypt_json(self, payload: bytes, aad: bytes = b"payload:v1") -> dict[str, Any]:
        _conn, box = self.require_open()
        raw = box.decrypt(payload, aad)
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Encrypted JSON payload was not an object.")
        return decoded

    def save_setting(self, key: str, value: dict[str, Any]) -> None:
        conn, _box = self.require_open()
        with self.lock:
            conn.execute(
                """
                INSERT INTO settings (key, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (key, self.encrypt_json(value), utc_now()),
            )
            conn.commit()

    def load_setting(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        conn, _box = self.require_open()
        row = conn.execute("SELECT payload FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return dict(default or {})
        try:
            return self.decrypt_json(row["payload"])
        except Exception:
            return dict(default or {})

    def add_event(self, level: str, message: str, **extra: Any) -> None:
        conn, _box = self.require_open()
        payload = {"message": clean_text(message, max_chars=2000, keep_newlines=True), "extra": extra}
        with self.lock:
            conn.execute(
                "INSERT INTO events (level, payload, created_at) VALUES (?, ?, ?)",
                (clean_text(level, max_chars=20), self.encrypt_json(payload), utc_now()),
            )
            conn.commit()

    def save_segment(
        self,
        *,
        channel: str,
        started_at: str,
        duration_seconds: float,
        transcript: str,
        refined_transcript: str,
        audio_sha256: str,
        status: str,
    ) -> int:
        conn, _box = self.require_open()
        payload = {
            "channel": channel,
            "started_at": started_at,
            "duration_seconds": duration_seconds,
            "transcript": clean_text(transcript, max_chars=30000, keep_newlines=True),
            "refined_transcript": clean_text(refined_transcript, max_chars=30000, keep_newlines=True),
            "audio_sha256": audio_sha256,
            "status": clean_text(status, max_chars=80),
        }
        with self.lock:
            cur = conn.execute(
                "INSERT INTO transcript_segments (channel, payload, created_at) VALUES (?, ?, ?)",
                (channel, self.encrypt_json(payload), utc_now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_segments(self, limit: int = 80) -> list[TranscriptSegment]:
        conn, _box = self.require_open()
        rows = conn.execute(
            """
            SELECT id, channel, payload, created_at
            FROM transcript_segments
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        records: list[TranscriptSegment] = []
        for row in reversed(rows):
            try:
                payload = self.decrypt_json(row["payload"])
            except Exception:
                continue
            records.append(
                TranscriptSegment(
                    id=int(row["id"]),
                    channel=str(payload.get("channel") or row["channel"]),
                    started_at=str(payload.get("started_at") or row["created_at"]),
                    duration_seconds=float(payload.get("duration_seconds") or 0),
                    transcript=str(payload.get("transcript") or ""),
                    refined_transcript=str(payload.get("refined_transcript") or ""),
                    audio_sha256=str(payload.get("audio_sha256") or ""),
                    status=str(payload.get("status") or ""),
                )
            )
        return records

    def save_bucket(
        self,
        *,
        kind: str,
        title: str,
        summary: str,
        confidence: float,
        stitch_key: str,
        source_segment_ids: list[int],
        details: dict[str, Any] | None = None,
    ) -> int:
        conn, _box = self.require_open()
        safe_kind = kind if kind in BUCKET_KINDS else "recipe_candidate"
        safe_stitch = clean_text(stitch_key or stable_slug(title), max_chars=120)
        payload = {
            "kind": safe_kind,
            "title": clean_text(title, max_chars=180),
            "summary": clean_text(summary, max_chars=4000, keep_newlines=True),
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "stitch_key": safe_stitch,
            "source_segment_ids": [int(x) for x in source_segment_ids],
            "details": details or {},
        }
        with self.lock:
            cur = conn.execute(
                "INSERT INTO buckets (kind, stitch_key, payload, created_at) VALUES (?, ?, ?, ?)",
                (safe_kind, safe_stitch, self.encrypt_json(payload), utc_now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_buckets(self, limit: int = 120, include_discard: bool = False) -> list[BucketRecord]:
        conn, _box = self.require_open()
        where = "" if include_discard else "WHERE kind != 'discard'"
        rows = conn.execute(
            f"""
            SELECT id, kind, stitch_key, payload, created_at
            FROM buckets
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        records: list[BucketRecord] = []
        for row in reversed(rows):
            try:
                payload = self.decrypt_json(row["payload"])
            except Exception:
                continue
            records.append(
                BucketRecord(
                    id=int(row["id"]),
                    kind=str(payload.get("kind") or row["kind"]),
                    title=str(payload.get("title") or ""),
                    summary=str(payload.get("summary") or ""),
                    confidence=float(payload.get("confidence") or 0),
                    stitch_key=str(payload.get("stitch_key") or row["stitch_key"]),
                    source_segment_ids=[int(x) for x in payload.get("source_segment_ids") or []],
                    created_at=str(row["created_at"]),
                )
            )
        return records

    def save_recipe(self, *, title: str, markdown: str, source_bucket_ids: list[int], metadata: dict[str, Any] | None = None) -> int:
        conn, _box = self.require_open()
        safe_title = clean_text(title or "Untitled Stream Recipe", max_chars=180)
        slug = stable_slug(safe_title)
        now = utc_now()
        payload = {
            "title": safe_title,
            "slug": slug,
            "markdown": clean_text(markdown, max_chars=60000, keep_newlines=True),
            "source_bucket_ids": [int(x) for x in source_bucket_ids],
            "metadata": metadata or {},
        }
        with self.lock:
            existing = conn.execute("SELECT id FROM recipes WHERE slug = ?", (slug,)).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO recipes (slug, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (slug, self.encrypt_json(payload), now, now),
                )
                recipe_id = int(cur.lastrowid)
            else:
                recipe_id = int(existing["id"])
                conn.execute(
                    "UPDATE recipes SET payload = ?, updated_at = ? WHERE id = ?",
                    (self.encrypt_json(payload), now, recipe_id),
                )
            stale = conn.execute(
                """
                SELECT id FROM recipes
                ORDER BY updated_at DESC, id DESC
                LIMIT -1 OFFSET ?
                """,
                (MAX_RECIPES,),
            ).fetchall()
            for row in stale:
                conn.execute("DELETE FROM recipes WHERE id = ?", (int(row["id"]),))
            conn.commit()
            return recipe_id

    def list_recipes(self, limit: int = 200) -> list[RecipeRecord]:
        conn, _box = self.require_open()
        rows = conn.execute(
            """
            SELECT id, slug, payload, created_at, updated_at
            FROM recipes
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, MAX_RECIPES)),),
        ).fetchall()
        recipes: list[RecipeRecord] = []
        for row in rows:
            try:
                payload = self.decrypt_json(row["payload"])
            except Exception:
                continue
            recipes.append(
                RecipeRecord(
                    id=int(row["id"]),
                    title=str(payload.get("title") or "Untitled Stream Recipe"),
                    slug=str(payload.get("slug") or row["slug"]),
                    markdown=str(payload.get("markdown") or ""),
                    source_bucket_ids=[int(x) for x in payload.get("source_bucket_ids") or []],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        return recipes


class GemmaAgent:
    def __init__(self, store: EncryptedRecipeStore, reporter: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.reporter = reporter or (lambda _msg: None)
        self._litert_lm: Any | None = None
        self._litert_error: Exception | None = None

    def _report(self, message: str) -> None:
        self.reporter(message)

    def _import_litert(self) -> Any:
        if self._litert_lm is not None:
            return self._litert_lm
        if self._litert_error is not None:
            raise RuntimeError(f"LiteRT-LM is unavailable: {self._litert_error}")
        try:
            import litert_lm as module  # type: ignore
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            self._litert_error = exc
            raise RuntimeError(f"LiteRT-LM is unavailable: {exc}") from exc
        self._litert_lm = module
        return module

    def model_status(self) -> str:
        if ENCRYPTED_MODEL_PATH.exists():
            return f"sealed model: {ENCRYPTED_MODEL_PATH}"
        if MODEL_PATH.exists():
            return f"plain model: {MODEL_PATH}"
        return "model missing"

    def _encrypt_model_file(self, plain_path: Path) -> None:
        _conn, box = self.store.require_open()
        tmp = ENCRYPTED_MODEL_PATH.with_suffix(".tmp")
        with plain_path.open("rb") as src, tmp.open("wb") as dest:
            dest.write(MODEL_CHUNK_MAGIC)
            dest.write(struct.pack(">I", MODEL_CHUNK_SIZE))
            index = 0
            while True:
                chunk = src.read(MODEL_CHUNK_SIZE)
                if not chunk:
                    break
                aad = b"gemma:model:chunk:v1:" + index.to_bytes(8, "big")
                blob = box.encrypt(chunk, aad)
                dest.write(struct.pack(">I", len(blob)))
                dest.write(blob)
                index += 1
        tmp.replace(ENCRYPTED_MODEL_PATH)
        set_private_permissions(ENCRYPTED_MODEL_PATH)
        with contextlib.suppress(Exception):
            plain_path.unlink()

    def download_model(self, progress: Callable[[str], None] | None = None) -> str:
        ensure_dirs()
        progress = progress or self._report
        if ENCRYPTED_MODEL_PATH.exists():
            sha = self.verify_model()
            return f"Model already sealed. SHA256 {sha}"
        url = MODEL_REPO + MODEL_FILE
        tmp = MODEL_DIR / f"{MODEL_FILE}.download"
        digest = hashlib.sha256()
        progress("Downloading Gemma 4 E2B LiteRT model...")
        try:
            with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as handle:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        progress(f"Downloading Gemma: {human_size(done)} / {human_size(total)}")
            sha = digest.hexdigest()
            if sha.lower() != MODEL_SHA256.lower():
                tmp.unlink(missing_ok=True)
                raise ValueError(f"Gemma SHA mismatch. Expected {MODEL_SHA256}, got {sha}.")
            progress("Sealing Gemma model into the encrypted vault...")
            self._encrypt_model_file(tmp)
            return f"Gemma ready. SHA256 {sha}"
        finally:
            tmp.unlink(missing_ok=True)

    def verify_model(self) -> str:
        with self.unlocked_model_path() as path:
            sha = sha256_file(path)
        if sha.lower() != MODEL_SHA256.lower():
            raise ValueError(f"Model SHA mismatch. Expected {MODEL_SHA256}, got {sha}.")
        return sha

    @contextlib.contextmanager
    def unlocked_model_path(self) -> Iterable[Path]:
        ensure_dirs()
        if ENCRYPTED_MODEL_PATH.exists():
            _conn, box = self.store.require_open()
            fd, temp_name = tempfile.mkstemp(prefix="gemma_model.", suffix=".litertlm", dir=str(CHUNK_DIR))
            os.close(fd)
            temp = Path(temp_name)
            try:
                with ENCRYPTED_MODEL_PATH.open("rb") as src:
                    header = src.read(len(MODEL_CHUNK_MAGIC))
                    if header == MODEL_CHUNK_MAGIC:
                        chunk_size_raw = src.read(4)
                        if len(chunk_size_raw) != 4:
                            raise ValueError("Sealed model header is truncated.")
                        chunk_size = struct.unpack(">I", chunk_size_raw)[0]
                        if chunk_size <= 0 or chunk_size > 64 * 1024 * 1024:
                            raise ValueError("Sealed model chunk size is invalid.")
                        with temp.open("wb") as dest:
                            index = 0
                            while True:
                                size_raw = src.read(4)
                                if not size_raw:
                                    break
                                if len(size_raw) != 4:
                                    raise ValueError("Sealed model chunk length is truncated.")
                                size = struct.unpack(">I", size_raw)[0]
                                if size <= 0 or size > chunk_size + 512:
                                    raise ValueError("Sealed model chunk length is invalid.")
                                blob = src.read(size)
                                if len(blob) != size:
                                    raise ValueError("Sealed model chunk payload is truncated.")
                                aad = b"gemma:model:chunk:v1:" + index.to_bytes(8, "big")
                                dest.write(box.decrypt(blob, aad))
                                index += 1
                    else:
                        src.seek(0)
                        temp.write_bytes(box.decrypt(src.read(), b"gemma:model:v1"))
                set_private_permissions(temp)
                yield temp
            finally:
                temp.unlink(missing_ok=True)
            return
        if MODEL_PATH.exists():
            yield MODEL_PATH
            return
        raise FileNotFoundError("Gemma 4 E2B model is missing. Use the Model tab to download or place it in the model folder.")

    @contextlib.contextmanager
    def _engine(self, *, enable_audio: bool = False) -> Iterable[Any]:
        litert = self._import_litert()
        with self.unlocked_model_path() as model_path:
            cache = Path(tempfile.mkdtemp(prefix="litert_cache.", dir=str(CHUNK_DIR)))
            try:
                try:
                    litert.set_min_log_severity(litert.LogSeverity.ERROR)
                except Exception:
                    pass
                backend = getattr(litert.Backend, "CPU", None)
                kwargs = {"cache_dir": str(cache)}
                if backend is not None:
                    kwargs["backend"] = backend
                    if enable_audio:
                        kwargs["audio_backend"] = backend
                engine = litert.Engine(str(model_path), **kwargs)
                with engine:
                    yield engine
            finally:
                shutil.rmtree(cache, ignore_errors=True)

    def chat(
        self,
        prompt: str,
        *,
        system: str = RECIPE_SYSTEM_PROMPT,
        on_delta: Callable[[str], None] | None = None,
        max_chars: int = 12000,
    ) -> str:
        clean_prompt = clean_text(prompt, max_chars=max_chars, keep_newlines=True)
        if not clean_prompt:
            return ""
        with self._engine() as engine:
            messages = [{"role": "system", "content": [{"type": "text", "text": system}]}] if system else []
            with engine.create_conversation(messages=messages) as conversation:
                stream_sender = None
                for name in ("send_message_stream", "send_message_streaming", "stream_message", "send_message_async"):
                    candidate = getattr(conversation, name, None)
                    if callable(candidate):
                        stream_sender = candidate
                        break
                if stream_sender is None or on_delta is None:
                    return self._response_to_text(conversation.send_message(clean_prompt))
                accumulated = ""
                emitted = False
                try:
                    result = stream_sender(clean_prompt)
                except TypeError:
                    return self._response_to_text(conversation.send_message(clean_prompt))
                if isinstance(result, dict):
                    return self._response_to_text(result)
                try:
                    iterator = iter(result)
                except TypeError:
                    return self._response_to_text(result)
                for chunk in iterator:
                    text = self._response_to_text(chunk)
                    if not text:
                        continue
                    if text.startswith(accumulated):
                        delta = text[len(accumulated) :]
                        accumulated = text
                    else:
                        delta = text
                        accumulated += text
                    if delta:
                        emitted = True
                        on_delta(delta)
                return clean_text(accumulated, max_chars=12000, keep_newlines=True) if emitted else ""

    @staticmethod
    def _response_to_text(response: Any) -> str:
        if isinstance(response, dict):
            parts = response.get("content") or []
            texts: list[str] = []
            for item in parts:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
            return clean_text("".join(texts), max_chars=16000, keep_newlines=True)
        return clean_text(response, max_chars=16000, keep_newlines=True)

    def transcribe_audio_with_gemma(self, audio_path: Path) -> str:
        """Attempt native multimodal audio transcription if the local LiteRT build supports it."""
        with self._engine(enable_audio=True) as engine:
            messages = [{"role": "system", "content": [{"type": "text", "text": TRANSCRIBE_SYSTEM_PROMPT}]}]
            with engine.create_conversation(messages=messages) as conversation:
                user_message = {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Transcribe this live cooking stream audio chunk. Preserve food words, "
                                "measurements, timing, temperatures, corrections, and uncertainty."
                            ),
                        },
                        {"type": "audio", "path": str(audio_path)},
                    ],
                }
                return self._response_to_text(conversation.send_message(user_message))

    def refine_transcript(self, transcript: str) -> str:
        clean = clean_text(transcript, max_chars=6000, keep_newlines=True)
        if not clean:
            return ""
        prompt = f"""
Clean this Twitch cooking-stream transcript as a culinary evidence document.

Refinement rules:
- Keep wording, ingredient names, quantities, temperatures, timings, and corrections faithful.
- Fix punctuation and sentence breaks where ASR clearly ran clauses together.
- Do not add new facts or recipe steps.
- Preserve meaningful repetition when it indicates correction, emphasis, or a changed instruction.
- Remove only empty filler that carries no recipe information.
- Mark uncertain terms in brackets.
- Return plain transcript text only.

Transcript:
{clean}
""".strip()
        try:
            return self.chat(prompt, system=TRANSCRIBE_SYSTEM_PROMPT, max_chars=8000)
        except Exception:
            return clean

    def bucketize(self, segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
        text_blocks = []
        for segment in segments:
            text = segment.refined_transcript or segment.transcript
            if text:
                text_blocks.append(f"[segment {segment.id} | {segment.started_at}]\n{text}")
        transcript = "\n\n".join(text_blocks)
        if not transcript:
            return []
        prompt = f"""
Classify this live cooking transcript into durable recipe-context buckets.

Allowed bucket kinds: {", ".join(BUCKET_KINDS)}

For each bucket include:
- kind: one allowed bucket kind
- title: short human label
- summary: evidence-first explanation, not a recipe rewrite
- confidence: 0 to 1
- stitch_key: stable lowercase-kebab-case dish/component key
- source_segment_ids: segment ids that prove the bucket
- details:
  - ingredients: array of strings
  - quantities: array of strings
  - techniques: array of strings
  - timings: array of strings
  - temperatures: array of strings
  - equipment: array of strings
  - substitutions: array of strings
  - visual_cues: array of strings
  - safety_or_allergy_notes: array of strings
  - unknowns: array of strings

Advanced stitching policy:
- If a fragment is probably the same dish as an earlier fragment, reuse its conceptual stitch_key.
- If it sounds like a component of a larger dish, use a component key such as sauce-base or rice-finish.
- If the host corrects themself, bucket the correction with higher confidence than the earlier phrase.
- If the text is chat, sponsorship, unrelated talk, or too ambiguous, use kind="discard".

Return JSON only:
{{"buckets":[...]}}

Transcript window:
{transcript[-MAX_TRANSCRIPT_WINDOW_CHARS:]}
""".strip()
        try:
            raw = self.chat(prompt, system=BUCKET_SYSTEM_PROMPT, max_chars=15000)
            parsed = extract_json(raw)
            buckets = parsed.get("buckets") if isinstance(parsed, dict) else []
            if isinstance(buckets, list):
                return [b for b in buckets if isinstance(b, dict)]
        except Exception as exc:
            self._report(f"Gemma bucketizer unavailable, using heuristic buckets: {exc}")
        return heuristic_buckets(segments)

    def recipe_from_buckets(self, buckets: list[BucketRecord], on_delta: Callable[[str], None] | None = None) -> tuple[str, str]:
        if not buckets:
            return "", ""
        context_lines = []
        for bucket in buckets:
            context_lines.append(
                f"[bucket {bucket.id} | {bucket.kind} | confidence {bucket.confidence:.2f} | key {bucket.stitch_key}]\n"
                f"Title: {bucket.title}\nSummary: {bucket.summary}"
            )
        context = "\n\n".join(context_lines)
        prompt = f"""
Turn these stitched live-stream cooking context buckets into a polished Markdown recipe entry.

Recipe requirements:
- H1 title that names the actual dish or component.
- Short source note: "Built from encrypted Twitch transcript context."
- Yield, prep time, cook time, and total time only when supported; otherwise write "Not stated".
- Ingredients grouped by component. Use "amount not stated" instead of inventing amounts.
- Numbered method with heat level, timing, sensory cues, and order of operations.
- "Live Stream Notes" for corrections, substitutions, off-camera jumps, and host-specific wording.
- "Uncertainty" for unclear quantities, missing steps, or ambiguous ingredients.
- "Make It Work" with practical recovery tips only when implied by the captured technique.
- Storage/reheating notes only if the transcript implies leftovers, batch cooking, or make-ahead handling.

Quality bar:
- A viewer should be able to cook from it while knowing exactly which details are confirmed and which are not.
- Keep the Markdown clean, direct, and kitchen-usable.
- Do not mention implementation internals, database encryption, prompts, or model limitations.

Return Markdown only.

Context buckets:
{context[-MAX_TRANSCRIPT_WINDOW_CHARS:]}
""".strip()
        try:
            markdown = self.chat(prompt, system=RECIPE_SYSTEM_PROMPT, on_delta=on_delta, max_chars=15000)
        except Exception as exc:
            self._report(f"Gemma recipe writer unavailable, using deterministic recipe template: {exc}")
            markdown = heuristic_recipe_markdown(buckets)
        title = extract_markdown_title(markdown) or buckets[0].title or "Stream Recipe"
        return title, markdown


def extract_json(text: str) -> dict[str, Any]:
    candidate = clean_text(text, max_chars=20000, keep_newlines=True)
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?", "", candidate.strip(), flags=re.I).strip()
        candidate = re.sub(r"```$", "", candidate).strip()
    try:
        decoded = json.loads(candidate)
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.S)
        if not match:
            return {}
        decoded = json.loads(match.group(0))
        return decoded if isinstance(decoded, dict) else {}


def extract_markdown_title(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return clean_text(line[2:], max_chars=180)
    first = clean_text(markdown.splitlines()[0] if markdown.splitlines() else "", max_chars=180)
    return first.lstrip("#").strip()


FOOD_WORDS = {
    "cup",
    "cups",
    "tablespoon",
    "tablespoons",
    "tbsp",
    "teaspoon",
    "tsp",
    "gram",
    "grams",
    "kg",
    "ml",
    "liter",
    "litre",
    "salt",
    "pepper",
    "oil",
    "butter",
    "flour",
    "sugar",
    "garlic",
    "onion",
    "sauce",
    "stock",
    "chicken",
    "beef",
    "pork",
    "fish",
    "rice",
    "pasta",
    "oven",
    "pan",
    "bake",
    "roast",
    "fry",
    "simmer",
    "boil",
    "mix",
    "stir",
}


def heuristic_buckets(segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    for segment in segments:
        text = clean_text(segment.refined_transcript or segment.transcript, max_chars=5000, keep_newlines=True)
        if not text:
            continue
        lower = text.lower()
        hits = sorted(word for word in FOOD_WORDS if re.search(rf"\b{re.escape(word)}\b", lower))
        kind = "recipe_candidate" if hits else "discard"
        title = "Possible stream recipe" if hits else "Low-signal stream talk"
        confidence = min(0.9, 0.28 + len(hits) * 0.07) if hits else 0.2
        buckets.append(
            {
                "kind": kind,
                "title": title,
                "summary": text[:1200],
                "confidence": confidence,
                "stitch_key": "stream-recipe" if hits else "discard",
                "source_segment_ids": [segment.id],
                "details": {"matched_terms": hits, "heuristic": True},
            }
        )
    return buckets


def heuristic_recipe_markdown(buckets: list[BucketRecord]) -> str:
    lines = ["# Stream Recipe Draft", "", "_Generated from encrypted Twitch transcript buckets._", ""]
    lines.extend(["## What We Know", ""])
    for bucket in buckets:
        if bucket.kind == "discard":
            continue
        lines.append(f"- **{bucket.title or bucket.kind.title()}**: {bucket.summary}")
    lines.extend(
        [
            "",
            "## Ingredients",
            "",
            "- Quantities need confirmation from the stream audio.",
            "",
            "## Method",
            "",
            "1. Review the transcript notes above and confirm exact ingredient amounts.",
            "2. Follow the captured technique and timing cues in order.",
            "",
            "## Uncertainty",
            "",
            "- Gemma was unavailable, so this is a deterministic draft from transcript buckets.",
        ]
    )
    return "\n".join(lines)


class Transcriber:
    def __init__(self, agent: GemmaAgent, reporter: Callable[[str], None]) -> None:
        self.agent = agent
        self.reporter = reporter

    def transcribe(self, audio_path: Path, *, use_gemma_audio: bool, command_template: str = "") -> tuple[str, str]:
        if use_gemma_audio:
            try:
                text = self.agent.transcribe_audio_with_gemma(audio_path)
                if text:
                    return text, "gemma-native-audio"
            except Exception as exc:
                self.reporter(f"Gemma native audio transcription unavailable for this chunk: {exc}")

        command_template = command_template.strip() or os.environ.get("SUPERAGENT_TRANSCRIBE_CMD", "").strip()
        if command_template:
            text = self._run_template(command_template, audio_path)
            if text:
                return text, "external-command"

        text = self._try_known_transcribers(audio_path)
        if text:
            return text, "detected-whisper"

        raise RuntimeError(
            "No transcription backend found. Enable Gemma native audio if supported, or set SUPERAGENT_TRANSCRIBE_CMD "
            "to a command that prints transcript text and uses {audio} for the WAV path."
        )

    def _run_template(self, template: str, audio_path: Path) -> str:
        command = template.format(audio=str(audio_path))
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=240,
            check=False,
        )
        if result.returncode != 0:
            detail = clean_text(result.stderr or result.stdout, max_chars=800, keep_newlines=True)
            raise RuntimeError(f"Transcription command failed: {detail}")
        return clean_text(result.stdout, max_chars=30000, keep_newlines=True)

    def _try_known_transcribers(self, audio_path: Path) -> str:
        if shutil.which("whisper-cli"):
            out_base = audio_path.with_suffix("")
            command = ["whisper-cli", "-f", str(audio_path), "-nt", "-otxt", "-of", str(out_base)]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240, check=False)
            text_path = out_base.with_suffix(".txt")
            if result.returncode == 0 and text_path.exists():
                text = clean_text(text_path.read_text(encoding="utf-8", errors="replace"), max_chars=30000, keep_newlines=True)
                text_path.unlink(missing_ok=True)
                return text
            detail = clean_text(result.stderr or result.stdout, max_chars=500)
            self.reporter(f"whisper-cli did not produce a transcript: {detail}")

        if shutil.which("whisper"):
            out_dir = audio_path.parent / f"whisper_out_{audio_path.stem}"
            out_dir.mkdir(parents=True, exist_ok=True)
            command = [
                "whisper",
                str(audio_path),
                "--model",
                os.environ.get("SUPERAGENT_WHISPER_MODEL", "base"),
                "--language",
                os.environ.get("SUPERAGENT_WHISPER_LANGUAGE", "en"),
                "--fp16",
                "False",
                "--output_format",
                "txt",
                "--output_dir",
                str(out_dir),
            ]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600, check=False)
            text_path = out_dir / f"{audio_path.stem}.txt"
            if result.returncode == 0 and text_path.exists():
                text = clean_text(text_path.read_text(encoding="utf-8", errors="replace"), max_chars=30000, keep_newlines=True)
                shutil.rmtree(out_dir, ignore_errors=True)
                return text
            shutil.rmtree(out_dir, ignore_errors=True)
            detail = clean_text(result.stderr or result.stdout, max_chars=500)
            self.reporter(f"whisper did not produce a transcript: {detail}")
        return ""


class TwitchPipeline:
    def __init__(self, events: "queue.Queue[tuple[str, Any]]") -> None:
        self.events = events
        self.capture_stream: subprocess.Popen[bytes] | None = None
        self.ffmpeg_process: subprocess.Popen[bytes] | None = None
        self.preview_stream: subprocess.Popen[bytes] | None = None
        self.preview_player: subprocess.Popen[bytes] | None = None
        self.monitor_thread: threading.Thread | None = None
        self.watcher_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.active_dir: Path | None = None
        self.channel = "unknown"
        self.chunk_seconds = DEFAULT_CHUNK_SECONDS

    def tool_report(self) -> str:
        tools = []
        for tool in ("streamlink", "ffmpeg", "ffplay"):
            path = shutil.which(tool)
            tools.append(f"{tool}: {path or 'missing'}")
        return " | ".join(tools)

    def streamlink_command(self, url: str, quality: str) -> list[str]:
        return [
            "streamlink",
            "--loglevel",
            "warning",
            "--stdout",
            "--twitch-disable-ads",
            "--stream-segment-threads",
            "4",
            "--stream-segment-attempts",
            "10",
            "--stream-segment-timeout",
            "20",
            "--hls-live-edge",
            "8",
            "--hls-playlist-reload-attempts",
            "15",
            "--retry-streams",
            "10",
            "--retry-open",
            "10",
            "--ringbuffer-size",
            "256M",
            url,
            quality,
        ]

    def ffmpeg_segment_command(self, output_pattern: Path, chunk_seconds: int) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "segment",
            "-segment_time",
            str(max(10, min(int(chunk_seconds), 180))),
            "-reset_timestamps",
            "1",
            "-map",
            "0:a:0",
            str(output_pattern),
        ]

    def ffplay_command(self, volume: float, video: bool = False) -> list[str]:
        base = ["ffplay", "-autoexit"]
        if not video:
            base.append("-nodisp")
        base.extend(["-f", "mpegts", "-af", f"volume={volume:.1f}", "-fflags", "nobuffer", "-flags", "low_delay", "-"])
        return base

    def start_capture(self, url: str, quality: str, chunk_seconds: int) -> Path:
        if self.is_running:
            raise RuntimeError("Capture pipeline is already running.")
        missing = [tool for tool in ("streamlink", "ffmpeg") if shutil.which(tool) is None]
        if missing:
            raise RuntimeError("Install required tools first: " + ", ".join(missing))
        if not is_twitch_url(url):
            raise ValueError("Enter a full Twitch stream URL.")
        self.stop_event.clear()
        self.channel = channel_from_url(url)
        self.chunk_seconds = max(10, min(int(chunk_seconds), 180))
        session = CHUNK_DIR / f"{self.channel}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        session.mkdir(parents=True, exist_ok=True)
        set_private_permissions(session, directory=True)
        output_pattern = session / "chunk_%06d.wav"
        self.capture_stream = subprocess.Popen(
            self.streamlink_command(url, quality),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if self.capture_stream.stdout is None:
            raise RuntimeError("streamlink did not expose an audio pipe.")
        self.ffmpeg_process = subprocess.Popen(
            self.ffmpeg_segment_command(output_pattern, self.chunk_seconds),
            stdin=self.capture_stream.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.capture_stream.stdout.close()
        self.active_dir = session
        self.monitor_thread = threading.Thread(target=self._monitor_processes, daemon=True)
        self.monitor_thread.start()
        self.watcher_thread = threading.Thread(target=self._watch_chunks, args=(session,), daemon=True)
        self.watcher_thread.start()
        self.events.put(("status", f"Capture started for #{self.channel}; chunks every {self.chunk_seconds}s."))
        return session

    @property
    def is_running(self) -> bool:
        return bool(self.ffmpeg_process and self.ffmpeg_process.poll() is None)

    def start_preview(self, url: str, quality: str, volume: float, video: bool) -> None:
        if self.preview_player and self.preview_player.poll() is None:
            raise RuntimeError("Preview is already running.")
        missing = [tool for tool in ("streamlink", "ffplay") if shutil.which(tool) is None]
        if missing:
            raise RuntimeError("Install required preview tools first: " + ", ".join(missing))
        self.preview_stream = subprocess.Popen(
            self.streamlink_command(url, quality),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if self.preview_stream.stdout is None:
            raise RuntimeError("streamlink did not expose a preview pipe.")
        self.preview_player = subprocess.Popen(
            self.ffplay_command(volume, video=video),
            stdin=self.preview_stream.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.preview_stream.stdout.close()
        self.events.put(("status", "FFplay preview started."))

    def stop_preview(self) -> None:
        for process in (self.preview_player, self.preview_stream):
            if process and process.poll() is None:
                process.terminate()
        for process in (self.preview_player, self.preview_stream):
            if process and process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1.5)
            if process and process.poll() is None:
                process.kill()
        self.preview_player = None
        self.preview_stream = None
        self.events.put(("status", "FFplay preview stopped."))

    def stop_capture(self) -> None:
        self.stop_event.set()
        for process in (self.ffmpeg_process, self.capture_stream):
            if process and process.poll() is None:
                process.terminate()
        deadline = time.time() + 2.5
        for process in (self.ffmpeg_process, self.capture_stream):
            if process and process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    process.kill()
        self.ffmpeg_process = None
        self.capture_stream = None
        self.events.put(("status", "Capture pipeline stopped."))

    def _monitor_processes(self) -> None:
        while not self.stop_event.is_set():
            stream_code = self.capture_stream.poll() if self.capture_stream else None
            ffmpeg_code = self.ffmpeg_process.poll() if self.ffmpeg_process else None
            if stream_code is not None or ffmpeg_code is not None:
                details = []
                if stream_code is not None:
                    details.append(f"streamlink exited {stream_code}")
                    details.append(self._stderr_tail(self.capture_stream))
                if ffmpeg_code is not None:
                    details.append(f"ffmpeg exited {ffmpeg_code}")
                    details.append(self._stderr_tail(self.ffmpeg_process))
                self.events.put(("pipeline_stopped", clean_text("\n".join(details), max_chars=1600, keep_newlines=True)))
                return
            time.sleep(2.0)

    def _stderr_tail(self, process: subprocess.Popen[bytes] | None) -> str:
        if process is None or process.stderr is None:
            return ""
        try:
            data = process.stderr.read(4096)
        except Exception:
            return ""
        return data.decode("utf-8", errors="replace")[-1200:]

    def _watch_chunks(self, session: Path) -> None:
        seen: set[Path] = set()
        stable_sizes: dict[Path, tuple[int, float]] = {}
        while not self.stop_event.is_set():
            for path in sorted(session.glob("chunk_*.wav")):
                if path in seen:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                previous_size, previous_time = stable_sizes.get(path, (-1, 0.0))
                now = time.time()
                if size > 4096 and size == previous_size and now - previous_time >= 1.0:
                    seen.add(path)
                    self.events.put(("audio_chunk", path))
                else:
                    stable_sizes[path] = (size, now if size != previous_size else previous_time or now)
            time.sleep(1.0)


class SuperAgentApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        self.title(APP_NAME)
        self.geometry("1380x900")
        self.minsize(1100, 720)
        self.configure(bg="#10131a")
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.store = EncryptedRecipeStore(DB_PATH)
        self.agent = GemmaAgent(self.store, lambda msg: self.events.put(("status", msg)))
        self.transcriber = Transcriber(self.agent, lambda msg: self.events.put(("status", msg)))
        self.pipeline = TwitchPipeline(self.events)
        self.processing_lock = threading.Lock()
        self.pending_segment_ids: list[int] = []
        self.bucket_batch_counter = 0
        self.selected_recipe: RecipeRecord | None = None

        self.url_var = tk.StringVar(value=DEFAULT_STREAM_URL)
        self.quality_var = tk.StringVar(value=DEFAULT_QUALITY)
        self.video_preview_var = tk.BooleanVar(value=False)
        self.gemma_audio_var = tk.BooleanVar(value=True)
        self.chunk_seconds_var = tk.IntVar(value=DEFAULT_CHUNK_SECONDS)
        self.bucket_every_var = tk.IntVar(value=DEFAULT_BUCKET_EVERY)
        self.volume_var = tk.DoubleVar(value=1.0)
        self.transcribe_cmd_var = tk.StringVar(value=os.environ.get("SUPERAGENT_TRANSCRIBE_CMD", ""))
        self.twitch_client_id_var = tk.StringVar(value="")
        self.twitch_oauth_var = tk.StringVar(value="")

        self._configure_theme()
        self._build_ui()
        self.after(200, self.process_events)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log(self.pipeline.tool_report())
        self.log(f"Vault path: {DB_PATH}")
        self.log(f"Model status: {self.agent.model_status()}")

    def _configure_theme(self) -> None:
        style = ttk.Style()
        with contextlib.suppress(Exception):
            style.theme_use("clam")
        style.configure("TNotebook", background="#10131a", borderwidth=0)
        style.configure("TNotebook.Tab", background="#1b2230", foreground="#d9e6ff", padding=(14, 8))
        style.map("TNotebook.Tab", background=[("selected", "#253348")], foreground=[("selected", "#ffffff")])
        style.configure("Treeview", background="#0b0f16", foreground="#e8eefc", fieldbackground="#0b0f16", borderwidth=0)
        style.configure("Treeview.Heading", background="#172030", foreground="#d9e6ff", relief="flat")

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = tk.Frame(self, bg="#151b26", width=330)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)

        tk.Label(sidebar, text="SuperAgentLLM", bg="#151b26", fg="#ffffff", font=("TkDefaultFont", 18, "bold")).pack(
            anchor="w", padx=18, pady=(18, 2)
        )
        tk.Label(sidebar, text="Twitch stream to encrypted Markdown recipes", bg="#151b26", fg="#9fb0c8", wraplength=285).pack(
            anchor="w", padx=18, pady=(0, 16)
        )

        self._labeled_entry(sidebar, "Twitch Stream", self.url_var)
        self._labeled_entry(sidebar, "Quality", self.quality_var)
        self._labeled_spin(sidebar, "Chunk seconds", self.chunk_seconds_var, 10, 180)
        self._labeled_spin(sidebar, "Bucket every N chunks", self.bucket_every_var, 1, 10)
        self._labeled_entry(sidebar, "Transcribe command ({audio})", self.transcribe_cmd_var)

        tk.Checkbutton(
            sidebar,
            text="Try Gemma native audio transcription first",
            variable=self.gemma_audio_var,
            bg="#151b26",
            fg="#d9e6ff",
            selectcolor="#0b0f16",
            activebackground="#151b26",
            activeforeground="#ffffff",
            wraplength=285,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(8, 4))
        tk.Checkbutton(
            sidebar,
            text="FFplay video preview",
            variable=self.video_preview_var,
            bg="#151b26",
            fg="#d9e6ff",
            selectcolor="#0b0f16",
            activebackground="#151b26",
            activeforeground="#ffffff",
        ).pack(anchor="w", padx=18, pady=(0, 10))

        volume_row = tk.Frame(sidebar, bg="#151b26")
        volume_row.pack(fill="x", padx=18, pady=(2, 12))
        tk.Label(volume_row, text="Preview Volume", bg="#151b26", fg="#b7c7dd").pack(anchor="w")
        tk.Scale(
            volume_row,
            from_=0.2,
            to=3.0,
            resolution=0.1,
            orient="horizontal",
            variable=self.volume_var,
            bg="#151b26",
            fg="#d9e6ff",
            highlightthickness=0,
            troughcolor="#293244",
        ).pack(fill="x")

        self.unlock_button = self._button(sidebar, "Unlock / Create Vault", self.unlock_vault, "#2d7ff9")
        self.start_button = self._button(sidebar, "Start Capture", self.start_capture, "#1f9d72", disabled=True)
        self.stop_button = self._button(sidebar, "Stop Capture", self.stop_capture, "#a33b4d", disabled=True)
        self.preview_button = self._button(sidebar, "Start FFplay Preview", self.toggle_preview, "#5661d8", disabled=True)
        self.agent_button = self._button(sidebar, "Run Recipe Agent Now", self.run_recipe_agent, "#a366ff", disabled=True)
        self.download_button = self._button(sidebar, "Download / Seal Gemma", self.download_model, "#3d6f8e", disabled=True)
        self.export_button = self._button(sidebar, "Export Selected Recipe", self.export_selected_recipe, "#445063", disabled=True)

        self.status_label = tk.Label(sidebar, text="Vault locked", bg="#151b26", fg="#ffcc66", wraplength=285, justify="left")
        self.status_label.pack(anchor="w", padx=18, pady=(16, 8))

        main = tk.Frame(self, bg="#10131a")
        main.grid(row=0, column=1, sticky="nsew", padx=14, pady=14)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        header = tk.Frame(main, bg="#10131a")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(header, text="Buzzmoo Live Recipe Intelligence", bg="#10131a", fg="#ffffff", font=("TkDefaultFont", 20, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.live_state = tk.Label(header, text="Idle", bg="#182131", fg="#9fb0c8", padx=12, pady=6)
        self.live_state.grid(row=0, column=1, sticky="e")

        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        self.transcript_text = self._text_tab("Live Transcript")
        self.bucket_tree, self.bucket_text = self._bucket_tab()
        self.recipe_list, self.recipe_text = self._recipe_tab()
        self.console_text = self._text_tab("Agent Console")
        self.settings_text = self._text_tab("System")

        self._write_system_panel()

    def _labeled_entry(self, parent: tk.Widget, label: str, variable: tk.StringVar) -> None:
        frame = tk.Frame(parent, bg="#151b26")
        frame.pack(fill="x", padx=18, pady=(0, 10))
        tk.Label(frame, text=label, bg="#151b26", fg="#b7c7dd").pack(anchor="w")
        entry = tk.Entry(frame, textvariable=variable, bg="#0b0f16", fg="#e8eefc", insertbackground="#ffffff", relief="flat")
        entry.pack(fill="x", ipady=7)

    def _labeled_spin(self, parent: tk.Widget, label: str, variable: tk.IntVar, from_: int, to: int) -> None:
        frame = tk.Frame(parent, bg="#151b26")
        frame.pack(fill="x", padx=18, pady=(0, 10))
        tk.Label(frame, text=label, bg="#151b26", fg="#b7c7dd").pack(anchor="w")
        spin = tk.Spinbox(
            frame,
            from_=from_,
            to=to,
            textvariable=variable,
            bg="#0b0f16",
            fg="#e8eefc",
            insertbackground="#ffffff",
            relief="flat",
        )
        spin.pack(fill="x", ipady=5)

    def _button(self, parent: tk.Widget, text: str, command: Callable[[], None], color: str, disabled: bool = False) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="#ffffff",
            activebackground=color,
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=9,
            state="disabled" if disabled else "normal",
        )
        button.pack(fill="x", padx=18, pady=(0, 8))
        return button

    def _text_tab(self, title: str) -> ScrolledText:
        frame = tk.Frame(self.notebook, bg="#10131a")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        text = ScrolledText(frame, bg="#0b0f16", fg="#e8eefc", insertbackground="#ffffff", wrap="word", relief="flat", padx=12, pady=12)
        text.grid(row=0, column=0, sticky="nsew")
        self.notebook.add(frame, text=title)
        return text

    def _bucket_tab(self) -> tuple[ttk.Treeview, ScrolledText]:
        frame = tk.Frame(self.notebook, bg="#10131a")
        frame.grid_columnconfigure(0, weight=2)
        frame.grid_columnconfigure(1, weight=3)
        frame.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("kind", "confidence", "stitch"), show="headings", height=16)
        tree.heading("kind", text="Kind")
        tree.heading("confidence", text="Confidence")
        tree.heading("stitch", text="Stitch Key")
        tree.column("kind", width=150)
        tree.column("confidence", width=95)
        tree.column("stitch", width=220)
        tree.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        text = ScrolledText(frame, bg="#0b0f16", fg="#e8eefc", insertbackground="#ffffff", wrap="word", relief="flat", padx=12, pady=12)
        text.grid(row=0, column=1, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", self.on_bucket_select)
        self.notebook.add(frame, text="Context Buckets")
        return tree, text

    def _recipe_tab(self) -> tuple[tk.Listbox, ScrolledText]:
        frame = tk.Frame(self.notebook, bg="#10131a")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=4)
        frame.grid_rowconfigure(0, weight=1)
        recipe_list = tk.Listbox(frame, bg="#0b0f16", fg="#e8eefc", selectbackground="#274766", relief="flat")
        recipe_list.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        text = ScrolledText(frame, bg="#0b0f16", fg="#e8eefc", insertbackground="#ffffff", wrap="word", relief="flat", padx=12, pady=12)
        text.grid(row=0, column=1, sticky="nsew")
        recipe_list.bind("<<ListboxSelect>>", self.on_recipe_select)
        self.notebook.add(frame, text="Markdown Recipes")
        return recipe_list, text

    def _write_system_panel(self) -> None:
        self.settings_text.delete("1.0", "end")
        lines = [
            f"App data: {APP_DIR}",
            f"Encrypted database: {DB_PATH}",
            f"Model folder: {MODEL_DIR}",
            f"Gemma model file: {MODEL_FILE}",
            f"Gemma status: {self.agent.model_status()}",
            f"Crypto provider: {'cryptography AES-GCM' if AESGCM is not None else 'stdlib HMAC stream fallback'}",
            f"cryptography import: {CRYPTO_IMPORT_ERROR or 'ok'}",
            self.pipeline.tool_report(),
            "",
            "Transcription backends:",
            "- Gemma native audio is attempted first when enabled and supported by your LiteRT build.",
            "- SUPERAGENT_TRANSCRIBE_CMD may be set to a command that prints text; use {audio} for the WAV file.",
            "- whisper-cli or whisper are auto-detected when installed.",
        ]
        self.settings_text.insert("end", "\n".join(lines))

    def unlock_vault(self) -> None:
        first_run = self.store.is_new
        prompt = "Create a vault password" if first_run else "Enter the vault password"
        password = simpledialog.askstring("Encrypted Recipe Vault", prompt, show="*", parent=self)
        if not password:
            return
        try:
            self.store.unlock(password)
            settings = self.store.load_setting("twitch", {})
            self.twitch_client_id_var.set(str(settings.get("client_id") or ""))
            self.twitch_oauth_var.set(str(settings.get("oauth") or ""))
        except Exception as exc:
            messagebox.showerror("Unlock failed", str(exc))
            self.log(f"Vault unlock failed: {exc}")
            return
        self.status_label.configure(text=f"Vault unlocked ({self.store.crypto_mode})", fg="#7ee0b2")
        for button in (self.start_button, self.preview_button, self.agent_button, self.download_button):
            button.configure(state="normal")
        self.refresh_all()
        self._write_system_panel()
        self.log(f"Vault unlocked with {self.store.crypto_mode}.")

    def start_capture(self) -> None:
        if self.store.conn is None:
            messagebox.showerror("Vault locked", "Unlock the encrypted vault first.")
            return
        try:
            session = self.pipeline.start_capture(
                self.url_var.get(),
                self.quality_var.get().strip() or DEFAULT_QUALITY,
                int(self.chunk_seconds_var.get()),
            )
        except Exception as exc:
            messagebox.showerror("Capture failed", str(exc))
            self.log(f"Capture start failed: {exc}")
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.live_state.configure(text="Capturing", fg="#7ee0b2")
        self.log(f"Chunk folder: {session}")

    def stop_capture(self) -> None:
        self.pipeline.stop_capture()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.live_state.configure(text="Idle", fg="#9fb0c8")

    def toggle_preview(self) -> None:
        if self.pipeline.preview_player and self.pipeline.preview_player.poll() is None:
            self.pipeline.stop_preview()
            self.preview_button.configure(text="Start FFplay Preview")
            return
        quality = self.quality_var.get().strip() or DEFAULT_QUALITY
        if self.video_preview_var.get() and quality == "audio_only":
            quality = DEFAULT_VIDEO_QUALITY
        try:
            self.pipeline.start_preview(self.url_var.get(), quality, float(self.volume_var.get()), bool(self.video_preview_var.get()))
        except Exception as exc:
            messagebox.showerror("Preview failed", str(exc))
            self.log(f"Preview failed: {exc}")
            return
        self.preview_button.configure(text="Stop FFplay Preview")

    def download_model(self) -> None:
        if self.store.conn is None:
            messagebox.showerror("Vault locked", "Unlock the encrypted vault first.")
            return
        self.download_button.configure(state="disabled")

        def worker() -> None:
            try:
                message = self.agent.download_model(lambda msg: self.events.put(("status", msg)))
                self.events.put(("status", message))
            except Exception as exc:
                self.events.put(("error", f"Model download failed: {exc}"))
            finally:
                self.events.put(("model_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def process_audio_chunk(self, audio_path: Path) -> None:
        if not self.processing_lock.acquire(blocking=False):
            self.events.put(("status", f"Processor is busy; queued chunk will wait: {audio_path.name}"))
            time.sleep(1.0)
            self.events.put(("audio_chunk", audio_path))
            return
        try:
            started_at = utc_now()
            audio_sha = sha256_file(audio_path)
            self.events.put(("status", f"Transcribing {audio_path.name}..."))
            transcript, backend = self.transcriber.transcribe(
                audio_path,
                use_gemma_audio=bool(self.gemma_audio_var.get()),
                command_template=self.transcribe_cmd_var.get(),
            )
            self.events.put(("status", f"Refining transcript from {backend}..."))
            refined = self.agent.refine_transcript(transcript)
            segment_id = self.store.save_segment(
                channel=self.pipeline.channel or channel_from_url(self.url_var.get()),
                started_at=started_at,
                duration_seconds=float(self.chunk_seconds_var.get()),
                transcript=transcript,
                refined_transcript=refined,
                audio_sha256=audio_sha,
                status=backend,
            )
            self.pending_segment_ids.append(segment_id)
            self.events.put(("segment_saved", segment_id))
            self.bucket_batch_counter += 1
            if self.bucket_batch_counter >= max(1, int(self.bucket_every_var.get())):
                self.bucket_batch_counter = 0
                self.run_recipe_agent(background=True)
        except Exception as exc:
            self.events.put(("error", f"Chunk processing failed for {audio_path.name}: {exc}"))
        finally:
            self.processing_lock.release()

    def run_recipe_agent(self, background: bool = False) -> None:
        if self.store.conn is None:
            if not background:
                messagebox.showerror("Vault locked", "Unlock the encrypted vault first.")
            return

        def worker() -> None:
            try:
                segments = self.store.list_segments(limit=24)
                recent = segments[-max(1, int(self.bucket_every_var.get()) * 2) :]
                if not recent:
                    self.events.put(("status", "No transcript segments are ready for the recipe agent."))
                    return
                self.events.put(("status", f"Bucketizing {len(recent)} transcript segments..."))
                raw_buckets = self.agent.bucketize(recent)
                bucket_ids: list[int] = []
                for raw in raw_buckets:
                    bucket_id = self.store.save_bucket(
                        kind=clean_text(raw.get("kind") or "recipe_candidate", max_chars=40),
                        title=clean_text(raw.get("title") or "Stream context", max_chars=180),
                        summary=clean_text(raw.get("summary") or "", max_chars=5000, keep_newlines=True),
                        confidence=float(raw.get("confidence") or 0.5),
                        stitch_key=clean_text(raw.get("stitch_key") or "stream-recipe", max_chars=120),
                        source_segment_ids=[int(x) for x in raw.get("source_segment_ids") or [recent[-1].id]],
                        details=raw.get("details") if isinstance(raw.get("details"), dict) else {},
                    )
                    bucket_ids.append(bucket_id)
                useful = [b for b in self.store.list_buckets(limit=80) if b.kind != "discard" and b.confidence >= 0.35]
                if useful:
                    grouped = group_recipe_buckets(useful)
                    for stitch_key, group in grouped.items():
                        if not group:
                            continue
                        self.events.put(("recipe_delta_clear", None))

                        def delta_sink(delta: str) -> None:
                            self.events.put(("recipe_delta", delta))

                        title, markdown = self.agent.recipe_from_buckets(group[-12:], on_delta=delta_sink)
                        if markdown:
                            self.store.save_recipe(
                                title=title,
                                markdown=markdown,
                                source_bucket_ids=[bucket.id for bucket in group[-12:]],
                                metadata={"stitch_key": stitch_key, "generated_at": utc_now()},
                            )
                self.events.put(("agent_done", {"buckets": len(bucket_ids)}))
            except Exception as exc:
                self.events.put(("error", f"Recipe agent failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all(self) -> None:
        if self.store.conn is None:
            return
        self.refresh_transcripts()
        self.refresh_buckets()
        self.refresh_recipes()

    def refresh_transcripts(self) -> None:
        self.transcript_text.delete("1.0", "end")
        for segment in self.store.list_segments(limit=120):
            text = segment.refined_transcript or segment.transcript
            self.transcript_text.insert(
                "end",
                f"[{segment.id}] #{segment.channel} {segment.started_at} ({segment.status})\n{text}\n\n",
            )
        self.transcript_text.see("end")

    def refresh_buckets(self) -> None:
        for item in self.bucket_tree.get_children():
            self.bucket_tree.delete(item)
        for bucket in self.store.list_buckets(limit=200):
            self.bucket_tree.insert(
                "",
                "end",
                iid=str(bucket.id),
                values=(bucket.kind, f"{bucket.confidence:.2f}", bucket.stitch_key),
                text=bucket.title,
            )
        self.bucket_text.delete("1.0", "end")
        self.bucket_text.insert("end", "Select a bucket to inspect title, summary, and source segment ids.")

    def refresh_recipes(self) -> None:
        self.recipe_list.delete(0, "end")
        recipes = self.store.list_recipes(limit=200)
        self.recipe_records = recipes
        for recipe in recipes:
            self.recipe_list.insert("end", recipe.title)
        if recipes and self.selected_recipe is None:
            self.recipe_list.selection_set(0)
            self.show_recipe(recipes[0])

    def on_bucket_select(self, _event: object | None = None) -> None:
        selected = self.bucket_tree.selection()
        if not selected or self.store.conn is None:
            return
        bucket_id = int(selected[0])
        bucket = next((b for b in self.store.list_buckets(limit=300, include_discard=True) if b.id == bucket_id), None)
        if bucket is None:
            return
        self.bucket_text.delete("1.0", "end")
        self.bucket_text.insert(
            "end",
            f"{bucket.title}\n\nKind: {bucket.kind}\nConfidence: {bucket.confidence:.2f}\n"
            f"Stitch key: {bucket.stitch_key}\nSources: {bucket.source_segment_ids}\nCreated: {bucket.created_at}\n\n"
            f"{bucket.summary}",
        )

    def on_recipe_select(self, _event: object | None = None) -> None:
        selected = self.recipe_list.curselection()
        if not selected:
            return
        index = int(selected[0])
        recipes = getattr(self, "recipe_records", [])
        if 0 <= index < len(recipes):
            self.show_recipe(recipes[index])

    def show_recipe(self, recipe: RecipeRecord) -> None:
        self.selected_recipe = recipe
        self.recipe_text.delete("1.0", "end")
        self.recipe_text.insert("end", recipe.markdown)
        self.export_button.configure(state="normal")

    def export_selected_recipe(self) -> None:
        if self.selected_recipe is None:
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Export Markdown Recipe",
            initialfile=f"{self.selected_recipe.slug}.md",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if not target:
            return
        Path(target).write_text(self.selected_recipe.markdown, encoding="utf-8")
        self.log(f"Exported recipe: {target}")

    def process_events(self) -> None:
        processed = 0
        while processed < 80:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if event == "status":
                self.log(str(payload))
            elif event == "error":
                self.log(str(payload), level="ERROR")
                self.live_state.configure(text="Needs attention", fg="#ffcc66")
            elif event == "pipeline_stopped":
                self.log(f"Pipeline stopped: {payload}", level="WARN")
                self.pipeline.stop_capture()
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.live_state.configure(text="Stopped", fg="#ff8585")
            elif event == "audio_chunk":
                threading.Thread(target=self.process_audio_chunk, args=(Path(payload),), daemon=True).start()
            elif event == "segment_saved":
                self.log(f"Encrypted transcript segment saved: #{payload}")
                self.refresh_transcripts()
            elif event == "agent_done":
                self.log(f"Recipe agent complete. Buckets saved: {payload.get('buckets', 0)}")
                self.refresh_buckets()
                self.refresh_recipes()
                self.live_state.configure(text="Capturing" if self.pipeline.is_running else "Idle", fg="#7ee0b2")
            elif event == "recipe_delta_clear":
                self.recipe_text.delete("1.0", "end")
            elif event == "recipe_delta":
                self.recipe_text.insert("end", str(payload))
                self.recipe_text.see("end")
            elif event == "model_done":
                self.download_button.configure(state="normal")
                self._write_system_panel()
            else:
                self.log(f"{event}: {payload}")
        self.after(200 if processed else 600, self.process_events)

    def log(self, message: str, *, level: str = "INFO") -> None:
        clean = clean_text(message, max_chars=3000, keep_newlines=True)
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {level}: {clean}\n"
        if hasattr(self, "console_text"):
            self.console_text.insert("end", line)
            self.console_text.see("end")
        if hasattr(self, "status_label") and level != "INFO":
            self.status_label.configure(text=clean[:260], fg="#ffcc66")
        try:
            if self.store.conn is not None:
                self.store.add_event(level.lower(), clean)
        except Exception:
            pass

    def on_close(self) -> None:
        self.pipeline.stop_preview()
        self.pipeline.stop_capture()
        self.store.close()
        self.destroy()


def group_recipe_buckets(buckets: list[BucketRecord]) -> dict[str, list[BucketRecord]]:
    grouped: dict[str, list[BucketRecord]] = {}
    for bucket in buckets:
        key = bucket.stitch_key or stable_slug(bucket.title)
        if bucket.kind in {"ingredient", "technique", "timing", "equipment", "substitution", "dietary", "serving", "recipe_candidate"}:
            grouped.setdefault(key, []).append(bucket)
    return grouped


def main() -> None:
    try:
        app = SuperAgentApp()
    except tk.TclError as exc:
        print(f"Could not start GUI: {exc}", file=sys.stderr)
        sys.exit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
