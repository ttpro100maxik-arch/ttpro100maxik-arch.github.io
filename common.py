"""Общие функции генератора Дзен-RSS: конфиг, время МСК, мелкие помощники."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
# Москва живёт в UTC+3 круглый год (без перевода часов с 2014 года).
MSK = timezone(timedelta(hours=3), "MSK")

DEFAULTS = {
    "base_url": "https://USERNAME.github.io/",
    "site_title": "Просто Максик",
    "site_description": "",
    "author": "Просто Максик",
    "language": "ru",
    "dzen_verification_meta": "",
    "verification_files": {},
    "categories": ["native-yes"],
    "feed_days": 7,
    "feed_from": None,
    "exclude_ids": [],
    "sources": ["../../content/*/дзен-*.md"],
    "prefer_v2_images": True,
    "inner_image_caption": "",
    "data_dir": "data",
    "output_dir": "docs",
}

BASE_TOKEN = "{{BASE}}"  # в body_html заменяется на base_url при рендере


def load_config(path=None):
    path = Path(path) if path else TOOL_DIR / "config.json"
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["_dir"] = str(path.resolve().parent)
    return cfg


def resolve(cfg, p):
    """Путь из конфига: абсолютный как есть, относительный — от папки конфига."""
    p = Path(p)
    return p if p.is_absolute() else Path(cfg["_dir"]) / p


def norm_base_url(url):
    url = (url or "").strip()
    if not url.endswith("/"):
        url += "/"
    return url


def parse_now(value=None):
    """'2026-10-28T20:00' (наивное = МСК) или с зоной. None → текущее время.
    Также читает переменную окружения DZEN_NOW."""
    value = value or os.environ.get("DZEN_NOW")
    if not value:
        return datetime.now(MSK)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def parse_slot(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt


_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def slugify(text, maxlen=60):
    s = "".join(_TR.get(ch, ch) for ch in text.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].rstrip("-") or "article"


def safe_filename(name):
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = slugify(stem, 100)
    return stem + ("." + ext.lower() if ext else "")
