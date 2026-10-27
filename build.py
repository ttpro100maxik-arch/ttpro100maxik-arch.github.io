#!/usr/bin/env python3
"""build.py — читает md-файлы Сценариста и собирает data/articles.json + data/img/.

Берёт только блоки «ДЗЕН · …», блоки «VK · …» игнорирует.
Запуск:  python3 build.py [--config config.json]
"""
import argparse
import glob
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from common import BASE_TOKEN, MSK, load_config, resolve, safe_filename, slugify

try:
    from PIL import Image  # необязательно: размеры картинок
except Exception:  # pragma: no cover
    Image = None

SEP_RE = re.compile(r"^\s*={10,}\s*$")
HDR_RE = re.compile(
    r"^ДЗЕН\s*·\s*(?:(\d{1,2})\.(\d{1,2})\s*·\s*)?(\d{1,2}):(\d{2})\s*(?:·\s*(.*?))?\s*$"
)
FNAME_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
PLACEHOLDER_RE = re.compile(r"^\[картинка внутрь:\s*(.*)\]\s*$")
INLINE_PLACEHOLDER_RE = re.compile(r"\[картинка внутрь:[^\]]*\]")
IMG_PATH_RE = re.compile(r"(\S+?\.(?:jpe?g|png|webp|gif))", re.I)
LIST_RE = re.compile(r"^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$")
HEAD_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*#*\s*$")

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif"}


# ---------- разметка ----------

def inline(text):
    """Экранирование + **жирный** + [ссылка](url)."""
    t = html.escape(text.strip(), quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: '<a href="%s">%s</a>' % (html.escape(m.group(2)), m.group(1)), t)
    return t


def figure_html(img, alt, caption):
    attrs = 'src="%simg/%s"' % (BASE_TOKEN, img["file"])
    if img.get("width") and img.get("height"):
        attrs += ' width="%d" height="%d"' % (img["width"], img["height"])
    attrs += ' alt="%s"' % html.escape(alt)
    cap = "<figcaption>%s</figcaption>" % html.escape(caption) if caption else ""
    return "<figure><img %s/>%s</figure>" % (attrs, cap)


def md_to_html(lines, image_resolver=None, alt="", caption="", log=None):
    """Простой markdown → чистый HTML: абзацы, ## заголовки, списки, плейсхолдеры картинок.
    image_resolver(path_text) → dict картинки или None (тогда плейсхолдер выкидываем)."""
    log = log if log is not None else []
    out, images = [], []
    para, lst = [], None  # lst = ("ul"|"ol", [items])

    def flush_para():
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para)))
            para.clear()

    def flush_list():
        nonlocal lst
        if lst:
            tag, items = lst
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(i) for i in items), tag))
            lst = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para(); flush_list()
            continue
        m = PLACEHOLDER_RE.match(line.strip())
        if m:
            flush_para(); flush_list()
            pm = IMG_PATH_RE.search(m.group(1))
            img = image_resolver(pm.group(1)) if (pm and image_resolver) else None
            if img and img.get("exists"):
                images.append(img)
                out.append(figure_html(img, alt, caption))
            else:
                log.append("картинка внутрь не найдена, блок убран: %s" % (pm.group(1) if pm else m.group(1)))
            continue
        line = INLINE_PLACEHOLDER_RE.sub("", line)
        h = HEAD_RE.match(line)
        if h:
            flush_para(); flush_list()
            level = max(2, min(len(h.group(1)), 4))  # h1 в тексте не нужен: он в заголовке
            out.append("<h%d>%s</h%d>" % (level, inline(h.group(2)), level))
            continue
        li = LIST_RE.match(line)
        if li:
            flush_para()
            tag = "ol" if li.group(1) else "ul"
            if lst and lst[0] != tag:
                flush_list()
            if not lst:
                lst = (tag, [])
            lst[1].append(li.group(2))
            continue
        flush_list()
        para.append(line.strip())
    flush_para(); flush_list()
    return "\n".join(out), images


def plain_description(body_lines, limit=300, min_len=120):
    """Первые абзацы текста (без заголовков и картинок), не короче min_len, не длиннее limit."""
    parts = []
    for line in body_lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("[картинка"):
            if parts and not s.startswith("["):
                if s.startswith("#"):
                    break
            continue
        parts.append(re.sub(r"\*\*(.+?)\*\*", r"\1", s))
        if len(" ".join(parts)) >= min_len:
            break
    s = " ".join(parts)
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—- ") + "…"


# ---------- разбор файлов ----------

def split_blocks(lines):
    """Файл = [преамбула] (=== шапка === тело)*. Возвращает [(шапка, тело)]."""
    seps = [i for i, l in enumerate(lines) if SEP_RE.match(l)]
    blocks, k = [], 0
    while k + 1 < len(seps):
        head = lines[seps[k] + 1:seps[k + 1]]
        end = seps[k + 2] if k + 2 < len(seps) else len(lines)
        blocks.append((head, lines[seps[k + 1] + 1:end]))
        k += 2
    return blocks


def parse_header(head_lines, file_date):
    head = [l.strip() for l in head_lines if l.strip()]
    if not head:
        return None
    m = HDR_RE.match(head[0])
    if not m:
        return None  # VK и прочее — не наше
    day, month, hh, mm, rubric = m.groups()
    year = file_date[0]
    if day:
        d, mo = int(day), int(month)
        if mo < file_date[1] and file_date[1] - mo > 6:
            year += 1  # переход через новый год
    else:
        d, mo = file_date[2], file_date[1]
    slot = datetime(year, mo, d, int(hh), int(mm), tzinfo=MSK)
    meta = {"slot": slot, "rubric": (rubric or "").strip("«» "), "title": "", "cover": "", "question": ""}
    for l in head[1:]:
        key, _, val = l.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "заголовок":
            meta["title"] = val
        elif key == "обложка":
            pm = IMG_PATH_RE.search(val)
            meta["cover"] = pm.group(1) if pm else val
        elif key == "вопрос читателя":
            meta["question"] = val.strip("«»")
    return meta


class ImageStore:
    """Находит исходную картинку, копирует в data/img, запоминает размеры и пропуски."""

    def __init__(self, img_dir, prefer_v2=True, copy=True):
        self.img_dir = Path(img_dir)
        self.prefer_v2 = prefer_v2
        self.copy = copy
        self.missing = []  # (статья, роль, путь)
        self.cache = {}

    def get(self, md_path, rel, article_id, role):
        src = (Path(md_path).parent / rel)
        if self.prefer_v2:
            v2 = src.with_name(src.stem + "-v2" + src.suffix)
            if v2.exists():
                src = v2
        key = str(src)
        if key in self.cache:
            return self.cache[key]
        placeholder = "%s/%s" % (Path(md_path).parent.name, rel)
        info = {"src": str(src), "placeholder": placeholder, "file": safe_filename(src.name),
                "type": MIME.get(src.suffix.lower(), "image/jpeg"),
                "exists": src.exists(), "bytes": 0, "width": None, "height": None}
        if info["exists"]:
            info["bytes"] = src.stat().st_size
            if Image is not None:
                try:
                    with Image.open(src) as im:
                        info["width"], info["height"] = im.size
                except Exception:
                    pass
            if self.copy:
                self.img_dir.mkdir(parents=True, exist_ok=True)
                dst = self.img_dir / info["file"]
                if not dst.exists() or dst.stat().st_size != info["bytes"]:
                    shutil.copy2(src, dst)
        else:
            self.missing.append((article_id, role, placeholder))
        self.cache[key] = info
        return info


def parse_file(md_path, store, caption="", log=None):
    log = log if log is not None else []
    md_path = Path(md_path)
    fm = FNAME_DATE_RE.search(md_path.name)
    if not fm:
        log.append("%s: в имени файла нет даты ГГГГ-ММ-ДД, пропускаю" % md_path.name)
        return []
    file_date = tuple(int(x) for x in fm.groups())
    lines = md_path.read_text(encoding="utf-8").splitlines()
    arts = []
    for head, body in split_blocks(lines):
        meta = parse_header(head, file_date)
        if not meta:
            continue
        if not meta["title"]:
            log.append("%s: блок %s без «Заголовок:», пропускаю" % (md_path.name, meta["slot"]))
            continue
        slot = meta["slot"]
        aid = slot.strftime("%Y-%m-%d-%H%M")
        if meta["cover"]:
            stem = Path(meta["cover"]).stem
            stem = re.sub(r"^dzen-\d{4}-\d{2}-\d{2}-", "", stem)
            slug = "%s-%s" % (slot.strftime("%Y-%m-%d"), slugify(stem))
        else:
            slug = "%s-%s" % (slot.strftime("%Y-%m-%d"), slugify(meta["title"], 50))
        cover = store.get(md_path, meta["cover"], aid, "обложка") if meta["cover"] else None
        if not meta["cover"]:
            log.append("%s: нет строки «Обложка:»" % aid)

        def resolver(rel, _md=md_path, _aid=aid):
            return store.get(_md, rel, _aid, "внутрь")

        alog = []
        body_html, images = md_to_html(body, resolver, alt=meta["title"], caption=caption, log=alog)
        log.extend("%s: %s" % (aid, x) for x in alog)
        arts.append({
            "id": aid,
            "slug": slug,
            "slot": slot.isoformat(),
            "title": meta["title"],
            "rubric": meta["rubric"],
            "question": meta["question"],
            "description": plain_description(body),
            "body_html": body_html,
            "cover": cover,
            "images": images,
            "source": md_path.name,
        })
    return arts


def build(cfg, copy_images=True, log=None):
    log = log if log is not None else []
    data_dir = resolve(cfg, cfg["data_dir"])
    store = ImageStore(data_dir / "img", cfg.get("prefer_v2_images", True), copy=copy_images)
    files = []
    for pattern in cfg["sources"]:
        files += glob.glob(str(resolve(cfg, pattern)))
    articles = []
    for f in sorted(set(files)):
        articles += parse_file(f, store, cfg.get("inner_image_caption", ""), log)
    articles.sort(key=lambda a: a["slot"])
    seen = {}
    for a in articles:
        if a["id"] in seen:
            log.append("ВНИМАНИЕ: два материала на один слот %s (%s и %s), второй получает суффикс"
                       % (a["id"], seen[a["id"]], a["source"]))
            a["id"] += "-2"
            a["slug"] += "-2"
        seen[a["id"]] = a["source"]
        if len(a["title"]) > 200:
            log.append("%s: заголовок длиннее 200 знаков (%d)" % (a["id"], len(a["title"])))
    return articles, store.missing


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    log = []
    articles, missing = build(cfg, log=log)
    data_dir = resolve(cfg, cfg["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    for a in articles:  # путь к исходнику на диске Макса в репозиторий не кладём
        for img in ([a["cover"]] if a["cover"] else []) + a["images"]:
            img.pop("src", None)
    with open(data_dir / "articles.json", "w", encoding="utf-8") as f:
        json.dump({"articles": articles}, f, ensure_ascii=False, indent=1)
    rep = ["Нет картинок (%d):" % len(missing)] + ["  %s  %-8s %s" % m for m in missing]
    (data_dir / "missing-images.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("Статей: %d → %s" % (len(articles), data_dir / "articles.json"))
    for a in articles:
        print("  %s  %s" % (a["id"], a["title"][:90]))
    for l in log:
        print("  ! " + l)
    print("\n".join(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
