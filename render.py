#!/usr/bin/env python3
"""render.py — из data/articles.json собирает сайт docs/ и docs/rss.xml.

В выдачу попадают ТОЛЬКО «вышедшие» статьи: сейчас (МСК) ≥ время слота.
Запуск:  python3 render.py [--now 2026-10-28T20:00] [--out DIR] [--base-url URL] [--config F]
"""
import argparse
import html
import json
import shutil
import sys
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from common import BASE_TOKEN, load_config, norm_base_url, parse_now, parse_slot, resolve

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

CSS = """body{margin:0;background:#fafaf7;color:#1b1b1b;font:18px/1.6 Georgia,'PT Serif',serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 64px}
a{color:#0b5cad}h1{font-size:1.9em;line-height:1.25;margin:.4em 0}
h2{font-size:1.35em;margin:1.6em 0 .4em}img{max-width:100%;height:auto;display:block;border-radius:6px}
figure{margin:1.6em 0}figcaption{font-size:.8em;color:#666;margin-top:.4em}
.meta{color:#666;font-size:.85em}.list{list-style:none;padding:0}.list li{margin:0 0 1.2em}
.list a{font-size:1.1em;text-decoration:none}header.site{border-bottom:1px solid #ddd;margin-bottom:24px}
@media (prefers-color-scheme:dark){body{background:#151515;color:#e8e8e8}a{color:#7db7ff}.meta,figcaption{color:#aaa}}"""


def human_date(dt):
    return "%d %s %d, %02d:%02d" % (dt.day, MONTHS[dt.month - 1], dt.year, dt.hour, dt.minute)


def verification_meta(cfg):
    v = (cfg.get("dzen_verification_meta") or "").strip()
    if not v:
        return "<!-- dzen-verification: вставь метатег Дзена в config.json → dzen_verification_meta -->"
    if v.startswith("<"):
        return v
    return '<meta name="zen-verification" content="%s"/>' % html.escape(v)


def page(cfg, title, body, description="", canonical=""):
    return """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
{verif}
<title>{title}</title>
<meta name="description" content="{desc}"/>
{canon}<link rel="alternate" type="application/rss+xml" title="{site}" href="{base}rss.xml"/>
<style>{css}</style>
</head>
<body><main>
<header class="site"><p><a href="{base}">{site}</a></p></header>
{body}
</main></body>
</html>
""".format(lang=cfg.get("language", "ru"), verif=verification_meta(cfg), title=html.escape(title),
           desc=html.escape(description), base=cfg["base_url"], site=html.escape(cfg["site_title"]),
           canon=('<link rel="canonical" href="%s"/>\n' % canonical) if canonical else "",
           css=CSS, body=body)


def article_url(cfg, a):
    return "%sa/%s.html" % (cfg["base_url"], a["slug"])


def body_abs(cfg, a):
    return a["body_html"].replace(BASE_TOKEN, cfg["base_url"])


def select(articles, now, cfg):
    """→ (released, in_feed). Неопубликованные отсюда не выходят."""
    released = [a for a in articles if parse_slot(a["slot"]) <= now]
    released.sort(key=lambda a: a["slot"], reverse=True)
    excl = set(cfg.get("exclude_ids") or [])
    since = now - timedelta(days=int(cfg.get("feed_days", 7)))
    feed_from = parse_slot(cfg["feed_from"]) if cfg.get("feed_from") else None
    feed = [a for a in released
            if parse_slot(a["slot"]) >= since
            and a["id"] not in excl
            and (feed_from is None or parse_slot(a["slot"]) >= feed_from)]
    return released, feed


def cdata(s):
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def rss(cfg, feed):
    base = cfg["base_url"]
    last = format_datetime(parse_slot(feed[0]["slot"])) if feed else None
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"'
           ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/"'
           ' xmlns:atom="http://www.w3.org/2005/Atom">',
           "<channel>",
           "<title>%s</title>" % xml_escape(cfg["site_title"]),
           "<link>%s</link>" % xml_escape(base),
           "<description>%s</description>" % xml_escape(cfg.get("site_description") or cfg["site_title"]),
           "<language>%s</language>" % cfg.get("language", "ru"),
           '<atom:link href="%srss.xml" rel="self" type="application/rss+xml"/>' % xml_escape(base)]
    if last:
        out.append("<lastBuildDate>%s</lastBuildDate>" % last)
    for a in feed:
        url = article_url(cfg, a)
        out.append("<item>")
        out.append("<title>%s</title>" % xml_escape(a["title"]))
        out.append("<link>%s</link>" % xml_escape(url))
        out.append('<guid isPermaLink="true">%s</guid>' % xml_escape(url))
        out.append("<pubDate>%s</pubDate>" % format_datetime(parse_slot(a["slot"])))
        out.append('<media:rating scheme="urn:simple">nonadult</media:rating>')
        if cfg.get("author"):
            out.append("<dc:creator>%s</dc:creator>" % xml_escape(cfg["author"]))
        for c in cfg.get("categories") or []:
            out.append("<category>%s</category>" % xml_escape(c))
        c = a.get("cover")
        if c and c.get("exists"):
            out.append('<enclosure url="%s" type="%s" length="%d"/>'
                       % (xml_escape(base + "img/" + c["file"]), c["type"], c["bytes"]))
        if a.get("description"):
            out.append("<description>%s</description>" % cdata(a["description"]))
        out.append("<content:encoded>%s</content:encoded>" % cdata(body_abs(cfg, a)))
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


def article_page(cfg, a):
    dt = parse_slot(a["slot"])
    c = a.get("cover")
    cover = ""
    if c and c.get("exists"):
        dims = ' width="%d" height="%d"' % (c["width"], c["height"]) if c.get("width") else ""
        cover = '<figure><img src="%simg/%s"%s alt="%s"/></figure>' % (
            cfg["base_url"], c["file"], dims, html.escape(a["title"]))
    body = """<article>
<h1>{title}</h1>
<p class="meta"><time datetime="{iso}">{human}</time></p>
{cover}
<div class="content">
{content}
</div>
</article>""".format(title=html.escape(a["title"]), iso=a["slot"], human=human_date(dt),
                     cover=cover, content=body_abs(cfg, a))
    return page(cfg, a["title"], body, a.get("description", ""), article_url(cfg, a))


def index_page(cfg, released):
    items = "\n".join(
        '<li><a href="a/{slug}.html">{t}</a><br/><span class="meta">{d}</span></li>'.format(
            slug=a["slug"], t=html.escape(a["title"]), d=human_date(parse_slot(a["slot"])))
        for a in released)
    body = "<h1>%s</h1>\n<p>%s</p>\n<ul class=\"list\">\n%s\n</ul>" % (
        html.escape(cfg["site_title"]), html.escape(cfg.get("site_description", "")),
        items or "<li>Скоро здесь будут статьи.</li>")
    return page(cfg, cfg["site_title"], body, cfg.get("site_description", ""), cfg["base_url"])


def render(cfg, articles, out_dir, now, img_src_dir, log=None):
    log = log if log is not None else []
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # чистим то, что генерируем сами (CNAME и прочее не трогаем)
    for d in ("a", "img"):
        shutil.rmtree(out / d, ignore_errors=True)
    for f in ("index.html", "rss.xml"):
        (out / f).unlink() if (out / f).exists() else None
    (out / "a").mkdir()
    (out / "img").mkdir()
    (out / ".nojekyll").write_text("", encoding="utf-8")

    released, feed = select(articles, now, cfg)
    for a in released:
        for img in ([a["cover"]] if a.get("cover") else []) + a.get("images", []):
            if not img.get("exists"):
                log.append("%s: нет картинки %s" % (a["id"], img.get("placeholder") or img["file"]))
                continue
            src = Path(img_src_dir) / img["file"]
            if src.exists():
                shutil.copy2(src, out / "img" / img["file"])
            else:
                log.append("%s: файл %s отсутствует в %s" % (a["id"], img["file"], img_src_dir))
        (out / "a" / (a["slug"] + ".html")).write_text(article_page(cfg, a), encoding="utf-8")
    (out / "index.html").write_text(index_page(cfg, released), encoding="utf-8")
    (out / "rss.xml").write_text(rss(cfg, feed), encoding="utf-8")
    for name, content in (cfg.get("verification_files") or {}).items():
        if "/" not in name and not name.startswith("."):
            (out / name).write_text(content, encoding="utf-8")
    return released, feed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--now", help="ISO-время, без зоны = МСК (для проверки); по умолчанию сейчас")
    ap.add_argument("--out", help="папка вывода (по умолчанию output_dir из конфига)")
    ap.add_argument("--base-url")
    args = ap.parse_args()
    cfg = load_config(args.config)
    cfg["base_url"] = norm_base_url(args.base_url or cfg["base_url"])
    now = parse_now(args.now)
    data_dir = resolve(cfg, cfg["data_dir"])
    with open(data_dir / "articles.json", encoding="utf-8") as f:
        articles = json.load(f)["articles"]
    out = Path(args.out) if args.out else resolve(cfg, cfg["output_dir"])
    log = []
    released, feed = render(cfg, articles, out, now, data_dir / "img", log)
    print("Сейчас (МСК): %s" % now.strftime("%Y-%m-%d %H:%M"))
    print("Всего статей: %d, вышли: %d, в ленте (за %s дн.): %d, ждут: %d"
          % (len(articles), len(released), cfg.get("feed_days", 7), len(feed), len(articles) - len(released)))
    print("Вывод: %s" % out)
    for l in log:
        print("  ! " + l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
