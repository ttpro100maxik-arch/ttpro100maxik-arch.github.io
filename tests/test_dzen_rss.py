"""Тесты генератора Дзен-RSS. Запуск из tools/dzen-rss:  python3 -m unittest -v"""
import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

import build  # noqa: E402
import render  # noqa: E402
import validate  # noqa: E402
from common import MSK, load_config, parse_now  # noqa: E402

CONTENT = TOOL.parents[1] / "content" / "2026-10-27-запуск-v3"
BASE = "https://prostomaxik.github.io/"
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}


def make_cfg(tmp):
    cfg = load_config(TOOL / "config.json")
    cfg["_dir"] = str(TOOL)
    cfg["sources"] = [str(CONTENT / "дзен-*.md")]
    cfg["data_dir"] = str(Path(tmp) / "data")
    cfg["base_url"] = BASE
    cfg["feed_from"] = None  # тесты не зависят от боевого окна ленты
    cfg["exclude_ids"] = []
    return cfg


@unittest.skipUnless(CONTENT.exists(), "нет папки с настоящими статьями")
class RealFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cfg = make_cfg(cls.tmp)
        cls.log = []
        cls.articles, cls.missing = build.build(cls.cfg, copy_images=True, log=cls.log)
        cls.img_dir = Path(cls.cfg["data_dir"]) / "img"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def render_at(self, now_iso, **cfg_over):
        out = Path(tempfile.mkdtemp(dir=self.tmp))
        cfg = dict(self.cfg, **cfg_over)
        released, feed = render.render(cfg, self.articles, out, parse_now(now_iso), self.img_dir)
        return out, released, feed

    # --- разбор ---
    def test_parsed_all_dzen_articles(self):
        self.assertGreaterEqual(len(self.articles), 15)
        ids = [a["id"] for a in self.articles]
        self.assertEqual(ids[0], "2026-10-27-1900")
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(len({a["slug"] for a in self.articles}), len(self.articles))

    def test_slot_times_msk(self):
        a = {x["id"]: x for x in self.articles}
        self.assertEqual(datetime.fromisoformat(a["2026-10-30-0800"]["slot"]),
                         datetime(2026, 10, 30, 8, 0, tzinfo=MSK))
        self.assertTrue(a["2026-11-01-1900"]["slot"].endswith("+03:00"))

    def test_titles_and_no_vk_text(self):
        first = self.articles[0]
        self.assertTrue(first["title"].startswith("Фонарщики, телефонистки"))
        for a in self.articles:
            self.assertTrue(a["title"])
            self.assertLessEqual(len(a["title"]), 200)
            body = a["body_html"]
            self.assertNotIn("Подробности — в нашем Telegram", body)  # фраза VK
            self.assertNotIn("Снимки:", body)
            self.assertNotIn("Заголовок:", body)
            self.assertNotIn("Обложка:", body)
            self.assertNotIn("[картинка", body)
            self.assertNotIn("====", body)
            self.assertIn("<h2>", body)
            self.assertTrue(a["description"])

    def test_images_mapped(self):
        a = self.articles[0]
        self.assertTrue(a["cover"]["exists"])
        self.assertEqual(a["cover"]["file"], "dzen-2026-10-27-fonarshchik-ii-v2.jpg")  # v2 в приоритете
        self.assertEqual(len(a["images"]), 1)
        self.assertIn("<figure><img src=\"{{BASE}}img/", a["body_html"])
        self.assertTrue((self.img_dir / a["cover"]["file"]).exists())

    # --- выпуск по времени ---
    def test_release_gating(self):
        _, rel, feed = self.render_at("2026-10-28T20:00")
        self.assertEqual([a["id"] for a in rel], ["2026-10-28-1900", "2026-10-28-0800", "2026-10-27-1900"])
        _, rel, _ = self.render_at("2026-10-28T18:59")
        self.assertEqual(len(rel), 2)
        _, rel, _ = self.render_at("2026-10-28T19:00")  # ровно время слота — уже вышла
        self.assertEqual(len(rel), 3)
        _, rel, _ = self.render_at("2026-10-27T18:59")
        self.assertEqual(rel, [])

    def test_feed_window_7_days(self):
        _, rel, feed = self.render_at("2026-11-05T20:00")
        expected = sum(1 for a in self.articles if a["id"] <= "2026-11-05-1900")
        self.assertEqual(len(rel), expected)
        ids = {a["id"] for a in feed}
        self.assertNotIn("2026-10-27-1900", ids)  # старше 7 дней
        self.assertNotIn("2026-10-29-1900", ids)  # 7 дней назад = 29.10 20:00
        self.assertIn("2026-10-30-0800", ids)

    def test_feed_from_and_exclude(self):
        _, _, feed = self.render_at("2026-10-28T20:00", feed_from="2026-10-28T12:00",
                                    exclude_ids=[])
        self.assertEqual([a["id"] for a in feed], ["2026-10-28-1900"])
        _, _, feed = self.render_at("2026-10-28T20:00", exclude_ids=["2026-10-28-0800"])
        self.assertEqual(len(feed), 2)

    # --- лента ---
    def test_feed_is_valid_xml(self):
        out, _, _ = self.render_at("2026-10-28T20:00")
        root = ET.parse(out / "rss.xml").getroot()
        self.assertEqual(root.tag, "rss")
        self.assertEqual(root.get("version"), "2.0")
        items = root.findall("channel/item")
        self.assertEqual(len(items), 3)
        for it in items:
            self.assertTrue(it.findtext("link").startswith(BASE))
            self.assertEqual(it.findtext("guid"), it.findtext("link"))
            self.assertEqual(it.findtext("category"), "native-yes")
            enc = it.find("enclosure")
            self.assertTrue(enc.get("url").startswith(BASE + "img/"))
            self.assertTrue(int(enc.get("length")) > 0)
            body = it.findtext("content:encoded", namespaces=NS)
            self.assertIn("<p>", body)
            self.assertNotIn("{{BASE}}", body)
            page = out / it.findtext("link")[len(BASE):]
            self.assertTrue(page.exists())
        self.assertEqual(items[0].findtext("pubDate"), "Wed, 28 Oct 2026 19:00:00 +0300")

    def test_validator_passes(self):
        out, _, _ = self.render_at("2026-10-28T20:00")
        errors, warnings, stats = validate.validate(out, BASE, parse_now("2026-10-28T20:00"), ["native-yes"])
        self.assertEqual(errors, [])
        self.assertEqual(stats["items"], 3)

    def test_validator_catches_problems(self):
        out, _, _ = self.render_at("2026-10-28T20:00")
        (out / "a" / "2026-10-28-roditeli-internet.html").unlink()
        errors, _, _ = validate.validate(out, BASE, parse_now("2026-10-28T20:00"))
        self.assertTrue(any("страницы статьи нет" in e for e in errors))
        # «будущая» статья в ленте должна ловиться
        errors, _, _ = validate.validate(out, BASE, parse_now("2026-10-28T10:00"))
        self.assertTrue(any("в будущем" in e for e in errors))

    def test_unreleased_not_leaked(self):
        out, rel, _ = self.render_at("2026-10-28T20:00")
        released_ids = {a["id"] for a in rel}
        everything = ""
        for f in out.rglob("*"):
            if f.is_file() and f.suffix in (".html", ".xml"):
                everything += f.read_text(encoding="utf-8")
        img_files = {f.name for f in (out / "img").iterdir()}
        pages = {f.stem for f in (out / "a").iterdir()}
        for a in self.articles:
            if a["id"] in released_ids:
                continue
            self.assertNotIn(a["title"], everything)
            self.assertNotIn(a["slug"], everything)
            self.assertNotIn(a["slug"], pages)
            if a["cover"]:
                self.assertNotIn(a["cover"]["file"], img_files)
            first_par = a["body_html"].split("</p>")[0]
            self.assertNotIn(first_par, everything)
        self.assertEqual(len(pages), 3)

    def test_stale_pages_removed(self):
        out, rel, _ = self.render_at("2026-11-05T20:00")
        self.assertEqual(len(list((out / "a").iterdir())), len(rel))
        render.render(self.cfg, self.articles, out, parse_now("2026-10-28T20:00"), self.img_dir)
        self.assertEqual(len(list((out / "a").iterdir())), 3)


class Markup(unittest.TestCase):
    def test_md_to_html(self):
        lines = ["Абзац <один> **жирно**", "", "## Заголовок", "", "- раз", "- два", "",
                 "1. первый", "2. второй", "", "[картинка внутрь: картинки/x.jpg (промпт: ...)]", "",
                 "Конец"]
        img = {"file": "x.jpg", "exists": True, "width": 800, "height": 600}
        log = []
        h, imgs = build.md_to_html(lines, lambda p: img, alt="T", caption="Создано ИИ", log=log)
        self.assertIn("<p>Абзац &lt;один&gt; <b>жирно</b></p>", h)
        self.assertIn("<h2>Заголовок</h2>", h)
        self.assertIn("<ul><li>раз</li><li>два</li></ul>", h)
        self.assertIn("<ol><li>первый</li><li>второй</li></ol>", h)
        self.assertIn('width="800" height="600"', h)
        self.assertIn("<figcaption>Создано ИИ</figcaption>", h)
        self.assertEqual(imgs, [img])

    def test_missing_image_dropped(self):
        log = []
        h, imgs = build.md_to_html(["[картинка внутрь: картинки/none.jpg (промпт)]"],
                                   lambda p: {"file": "none.jpg", "exists": False}, log=log)
        self.assertEqual(h, "")
        self.assertEqual(imgs, [])
        self.assertTrue(log)

    def test_vk_block_ignored(self):
        lines = ["Публикация: ...", "=" * 20, "VK · после 19:00 · пост", "=" * 20, "Текст VK",
                 "=" * 20, "ДЗЕН · 08:00 · «Было»", "Заголовок: Тест", "=" * 20, "Текст Дзена"]
        blocks = build.split_blocks(lines)
        self.assertEqual(len(blocks), 2)
        self.assertIsNone(build.parse_header(blocks[0][0], (2026, 10, 28)))
        meta = build.parse_header(blocks[1][0], (2026, 10, 28))
        self.assertEqual(meta["title"], "Тест")
        self.assertEqual(meta["slot"], datetime(2026, 10, 28, 8, 0, tzinfo=MSK))


if __name__ == "__main__":
    unittest.main()
