#!/usr/bin/env python3
"""validate.py — проверяет docs/rss.xml и сайт по требованиям Дзена до отправки ленты.

Запуск:  python3 validate.py [--out DIR] [--base-url URL] [--now ISO] [--config F]
Код выхода 0 — ошибок нет (предупреждения допустимы), 1 — есть ошибки.
"""
import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from common import load_config, norm_base_url, parse_now, resolve

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
}
MAX_FEED = 10 * 1024 * 1024
MAX_ENCLOSURE = 25 * 1024 * 1024
MAX_ITEMS = 500


def local_path(out, base, url):
    """Абсолютный URL сайта → файл в out (или None, если домен чужой)."""
    if not url.startswith(base):
        return None
    rel = url[len(base):].split("#")[0].split("?")[0]
    return Path(out) / (rel or "index.html")


def validate(out, base, now=None, categories=None):
    """→ (errors, warnings, stats)."""
    out = Path(out)
    base = norm_base_url(base)
    E, W = [], []
    stats = {"items": 0}
    feed = out / "rss.xml"
    if not feed.exists():
        return ["нет файла %s" % feed], W, stats
    raw = feed.read_bytes()
    stats["feed_bytes"] = len(raw)
    if len(raw) >= MAX_FEED:
        E.append("лента %d байт, нужно меньше 10 МБ" % len(raw))
    text = raw.decode("utf-8", errors="replace")
    for prefix, uri in NS.items():
        if 'xmlns:%s="%s"' % (prefix, uri) not in text:
            E.append("не объявлено пространство имён %s (%s)" % (prefix, uri))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return E + ["XML не разбирается: %s" % e], W, stats
    if root.tag != "rss" or root.get("version") != "2.0":
        E.append("корень должен быть <rss version=\"2.0\">")
    ch = root.find("channel")
    if ch is None:
        return E + ["нет <channel>"], W, stats
    for tag in ("title", "link", "description"):
        if not (ch.findtext(tag) or "").strip():
            E.append("в channel пустой <%s>" % tag)
    if "USERNAME" in base or "example" in base:
        W.append("base_url похож на заглушку: %s" % base)
    if not (ch.findtext("link") or "").startswith(base):
        E.append("channel/link не на домене сайта %s" % base)
    if (out / "index.html").exists():
        idx = (out / "index.html").read_text(encoding="utf-8")
        if "<!-- dzen-verification:" in idx:
            W.append("в index.html нет метатега проверки Дзена (config → dzen_verification_meta)")
    else:
        E.append("нет index.html")

    items = ch.findall("item")
    stats["items"] = len(items)
    if not items:
        W.append("в ленте нет ни одной статьи (на модерацию подавать с готовыми статьями)")
    if len(items) > MAX_ITEMS:
        E.append("статей в ленте %d, максимум %d" % (len(items), MAX_ITEMS))
    guids = set()
    for n, it in enumerate(items, 1):
        title = (it.findtext("title") or "").strip()
        tag = "item %d «%s»" % (n, title[:40])
        if not title:
            E.append("%s: пустой title" % tag)
        elif len(title) > 200:
            E.append("%s: title %d знаков, максимум 200" % (tag, len(title)))
        link = (it.findtext("link") or "").strip()
        if not link.startswith("http"):
            E.append("%s: link не абсолютный: %r" % (tag, link))
        elif not link.startswith(base):
            E.append("%s: link не на домене сайта: %s" % (tag, link))
        guid = (it.findtext("guid") or "").strip()
        if not guid:
            E.append("%s: нет guid" % tag)
        elif guid in guids:
            E.append("%s: повтор guid %s" % (tag, guid))
        guids.add(guid)
        pd = (it.findtext("pubDate") or "").strip()
        try:
            dt = parsedate_to_datetime(pd)
            if dt.tzinfo is None:
                E.append("%s: pubDate без часового пояса: %s" % (tag, pd))
            elif now is not None:
                if dt > now:
                    E.append("%s: pubDate в будущем (%s) — неопубликованная статья в ленте" % (tag, pd))
                if dt < now - timedelta(days=7):
                    W.append("%s: старше 7 дней, Дзен её не возьмёт" % tag)
        except Exception:
            E.append("%s: pubDate не в формате RFC 822: %r" % (tag, pd))
        cats = [c.text for c in it.findall("category")]
        if not cats:
            W.append("%s: нет category (без неё статья выходит сразу)" % tag)
        if categories is not None and set(categories) - set(cats):
            E.append("%s: нет категорий %s" % (tag, sorted(set(categories) - set(cats))))
        # обложка
        enc = it.find("enclosure")
        if enc is None:
            E.append("%s: нет enclosure (обложки)" % tag)
        else:
            url, typ, length = enc.get("url", ""), enc.get("type", ""), enc.get("length", "")
            if not url.startswith("http"):
                E.append("%s: enclosure url не абсолютный" % tag)
            if not typ.startswith("image/"):
                E.append("%s: enclosure type %r, нужен image/*" % (tag, typ))
            lp = local_path(out, base, url)
            if lp is None or not lp.exists():
                E.append("%s: файла обложки нет на сайте: %s" % (tag, url))
            else:
                size = lp.stat().st_size
                if not length.isdigit() or int(length) != size:
                    E.append("%s: enclosure length=%s, а файл %d байт" % (tag, length, size))
                if size > MAX_ENCLOSURE:
                    E.append("%s: обложка больше 25 МБ" % tag)
                if Image is not None:
                    try:
                        with Image.open(lp) as im:
                            w, h = im.size
                        if w < 480 or h < 320:
                            E.append("%s: обложка %dx%d, нужно не меньше 480x320" % (tag, w, h))
                    except Exception as e:
                        E.append("%s: обложка не открывается: %s" % (tag, e))
                else:
                    W.append("Pillow нет — размеры обложек не проверены")
        # полный текст
        body = it.findtext("content:encoded", namespaces=NS) or ""
        if len(re.sub(r"<[^>]+>", "", body).strip()) < 300:
            E.append("%s: content:encoded пустой или слишком короткий" % tag)
        for src in re.findall(r'<img[^>]+src="([^"]+)"', body):
            src = html.unescape(src)
            if not src.startswith("http"):
                E.append("%s: картинка с относительным адресом %s" % (tag, src))
                continue
            lp = local_path(out, base, src)
            if lp is None or not lp.exists():
                E.append("%s: картинки нет на сайте: %s" % (tag, src))
        if re.search(r"\[картинка внутрь|\{\{BASE\}\}", body):
            E.append("%s: в тексте остался служебный плейсхолдер" % tag)
        for bad in re.findall(r"<(script|iframe|style)\b", body):
            E.append("%s: недопустимый тег <%s> в тексте" % (tag, bad))
        # страница статьи
        lp = local_path(out, base, link) if link else None
        if lp is None or not lp.exists():
            E.append("%s: страницы статьи нет: %s" % (tag, link))
        else:
            ph = lp.read_text(encoding="utf-8")
            if "<h1>%s</h1>" % html.escape(title) not in ph:
                E.append("%s: на странице другой заголовок" % tag)
            if body.strip() and body.strip() not in ph:
                E.append("%s: текст на странице не совпадает с content:encoded" % tag)
    W = list(dict.fromkeys(W))
    return E, W, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--out")
    ap.add_argument("--base-url")
    ap.add_argument("--now")
    args = ap.parse_args()
    cfg = load_config(args.config)
    base = norm_base_url(args.base_url or cfg["base_url"])
    out = Path(args.out) if args.out else resolve(cfg, cfg["output_dir"])
    E, W, st = validate(out, base, parse_now(args.now), cfg.get("categories"))
    print("Лента: %s, %d байт, статей: %d" % (out / "rss.xml", st.get("feed_bytes", 0), st["items"]))
    for w in W:
        print("  предупреждение: " + w)
    for e in E:
        print("  ОШИБКА: " + e)
    print("ИТОГ: %s" % ("OK, можно отправлять" if not E else "%d ошибок, не отправлять" % len(E)))
    return 1 if E else 0


if __name__ == "__main__":
    sys.exit(main())
