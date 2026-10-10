#!/usr/bin/env python3
"""VPS watcher: public Hatobox Mercari pages -> durable Telegram outbox."""
import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("hatobox")
CARD_JS = r"""cards => cards.map(card => {
  const a = card.querySelector('a.item-link-title');
  const img = card.querySelector('a.item-link-image img');
  return {
    title: a ? a.innerText.trim() : '',
    url: a ? a.href : '',
    price_jpy: card.querySelector('.price-sub')?.innerText || '',
    price_vnd: card.querySelector('.price-main')?.innerText || '',
    image_url: img ? (img.currentSrc || img.src) : ''
  };
})"""
BLOCK_RE = re.compile(r"verify (?:that )?you are human|checking your browser|unusual traffic|automated (?:traffic|queries)|robot verification|human verification", re.I)


class SourceError(Exception):
    pass


class SiteBlocked(SourceError):
    pass


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram error {code}")  # Never include token or URL.


def folded(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def regex_match(pattern, text):
    return re.search(folded(pattern), folded(text), re.I) is not None


def money(text):
    match = re.search(r"\d[\d,\.\s]*", str(text))
    return int(re.sub(r"\D", "", match.group())) if match else None


def item_from_row(row):
    parsed = urlparse(row.get("url", ""))
    match = re.fullmatch(r"/mercari/item/(m\d+)", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "hatobox.jp" or not match:
        raise SourceError("Đường dẫn sản phẩm không đúng cấu trúc Hatobox.")
    if not row.get("title", "").strip():
        raise SourceError("Không đọc được tên sản phẩm; kiểm tra giao diện nguồn.")
    jpy, vnd = money(row.get("price_jpy", "")), money(row.get("price_vnd", ""))
    if jpy is None or vnd is None:
        raise SourceError("Không đọc được giá ¥/VNĐ; chưa cập nhật mốc tin.")
    image = row.get("image_url", "")
    image_parsed = urlparse(image)
    if image_parsed.scheme != "https" or image_parsed.hostname != "static.mercdn.net":
        image = ""
    return {"id": "mercari:" + match.group(1), "title": row["title"].strip(),
            "url": "https://hatobox.jp" + parsed.path + "?type=mer",
            "jpy": jpy, "vnd": vnd, "image_url": image}


def group_for(item, config):
    if config.get("exclude_title") and regex_match(config["exclude_title"], item["title"]):
        return None
    for group in config["groups"]:
        if not regex_match(group["include"], item["title"]):
            continue
        if config["mode"] == "price_ceiling":
            limits = [("max_jpy", "jpy"), ("max_vnd", "vnd")]
            if not any(group.get(k) is not None for k, _ in limits):
                continue  # Never label an unpriced group as a cheap deal.
            if any(group.get(k) is not None and item[v] > group[k] for k, v in limits):
                continue
        return group["name"]
    return None


def read_config(path=ROOT / "config.json"):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("mode") not in ("new_only", "price_ceiling"):
        raise ValueError("mode phải là new_only hoặc price_ceiling")
    if not 30 <= config.get("poll_seconds", 0) <= 3600:
        raise ValueError("poll_seconds phải từ 30 tới 3600")
    if not 1 <= config.get("max_pages", 0) <= 10:
        raise ValueError("max_pages phải từ 1 tới 10")
    if not 10 <= config.get("page_timeout_seconds", 0) <= 120:
        raise ValueError("page_timeout_seconds phải từ 10 tới 120")
    if not 60 <= config.get("notification_expiry_seconds", 0) <= 86400:
        raise ValueError("notification_expiry_seconds phải từ 60 tới 86400")
    if not config.get("sources") or not config.get("groups"):
        raise ValueError("Thiếu sources hoặc groups")
    for source in config["sources"]:
        parsed = urlparse(source["url"])
        if parsed.scheme != "https" or parsed.netloc != "hatobox.jp":
            raise ValueError("Chỉ hỗ trợ nguồn công khai https://hatobox.jp")
        if not re.fullmatch(r"/mercari/(category/\d+|search)", parsed.path):
            raise ValueError("Bản này hỗ trợ danh mục/tìm kiếm Mercari qua Hatobox")
        query = parse_qs(parsed.query)
        expected = {"sort": "created_time", "order": "desc", "status": "1", "purchase_method": "instant"}
        if any(query.get(k) != [v] for k, v in expected.items()):
            raise ValueError("Nguồn cần sort=created_time, order=desc, status=1, purchase_method=instant")
        if query.get("page") != ["1"]:
            raise ValueError("Nguồn phải bắt đầu từ page=1")
        source["key"] = hashlib.sha256(source["url"].encode()).hexdigest()
    for group in config["groups"]:
        re.compile(folded(group["include"]), re.I)
        for field in ("max_jpy", "max_vnd"):
            if group.get(field) is not None and (type(group[field]) is not int or group[field] <= 0):
                raise ValueError(f"{field} phải là số nguyên dương hoặc null")
    re.compile(folded(config.get("exclude_title", "")), re.I)
    return config


def load_env():
    path = ROOT / ".env"
    if path.exists() and not all(os.getenv(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")):
        for line in path.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                    os.environ.setdefault(key, value.strip())
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"-?\d+", chat):
        raise ValueError("Chưa có kết nối Telegram. Chạy setup_telegram.py trên VPS.")
    return token, chat


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sources (key TEXT PRIMARY KEY, scanned REAL);
          CREATE TABLE IF NOT EXISTS seen (source TEXT, item TEXT, PRIMARY KEY(source,item));
          CREATE INDEX IF NOT EXISTS seen_item ON seen(item);
          CREATE TABLE IF NOT EXISTS outbox (item TEXT PRIMARY KEY, payload TEXT,
              created REAL, status TEXT DEFAULT 'pending');
        """)

    def initialized(self, key):
        return self.db.execute("SELECT 1 FROM sources WHERE key=?", (key,)).fetchone() is not None

    def known(self, key):
        return {r[0] for r in self.db.execute("SELECT item FROM seen WHERE source=? ORDER BY rowid DESC LIMIT 2000", (key,))}

    def record(self, source, observed, candidates, config, now=None):
        now = time.time() if now is None else now
        initialized = self.initialized(source["key"])
        added = 0
        with self.db:
            if initialized:
                for item in candidates:
                    if self.db.execute("SELECT 1 FROM seen WHERE item=? LIMIT 1", (item["id"],)).fetchone():
                        continue
                    group = group_for(item, config)
                    if not group:
                        continue
                    payload = {**item, "group": group, "source": source["name"], "detected": now}
                    cur = self.db.execute("INSERT OR IGNORE INTO outbox(item,payload,created) VALUES(?,?,?)",
                                          (item["id"], json.dumps(payload, ensure_ascii=False), now))
                    added += cur.rowcount
            self.db.executemany("INSERT OR IGNORE INTO seen(source,item) VALUES(?,?)",
                                [(source["key"], i["id"]) for i in observed])
            self.db.execute("INSERT INTO sources VALUES(?,?) ON CONFLICT(key) DO UPDATE SET scanned=excluded.scanned",
                            (source["key"], now))
        return initialized, added

    def pending(self, expiry, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND created<?", (now-expiry,))
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM outbox WHERE status='pending' ORDER BY created,item")]

    def sent(self, item):
        with self.db:
            self.db.execute("UPDATE outbox SET status='sent' WHERE item=?", (item,))

    def status(self):
        return {"sources": self.db.execute("SELECT count(*) FROM sources").fetchone()[0],
                "seen": self.db.execute("SELECT count(*) FROM seen").fetchone()[0],
                "outbox": dict(self.db.execute("SELECT status,count(*) FROM outbox GROUP BY status"))}


class Telegram:
    def __init__(self, token, chat=""):
        self._token, self.chat = token, chat
        self.retry_until = 0

    def call_sync(self, method, payload=None):
        request = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (ValueError, OSError):
                body = {}
            raise TelegramError(exc.code, body.get("parameters", {}).get("retry_after", 0)) from None
        except (OSError, ValueError):
            raise TelegramError("network") from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", "response"), body.get("parameters", {}).get("retry_after", 0))
        return body["result"]

    async def call(self, method, payload=None):
        if time.monotonic() < self.retry_until:
            raise TelegramError(429, int(self.retry_until - time.monotonic()) + 1)
        try:
            return await asyncio.to_thread(self.call_sync, method, payload)
        except TelegramError as exc:
            if exc.retry_after:
                self.retry_until = time.monotonic() + exc.retry_after
            raise

    async def text(self, text):
        return await self.call("sendMessage", {"chat_id": self.chat, "text": text})

    async def notify(self, item, photos=True):
        stamp = datetime.fromtimestamp(item["detected"], ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M:%S %d/%m")
        title = html.escape(item["title"][:190])
        caption = (f"🔔 <b>TIN MỚI PHÁT HIỆN</b>\n{html.escape(item['group'])}\n\n"
                   f"<b>{title}</b>\n💴 ¥{item['jpy']:,} · {item['vnd']:,} đ\n"
                   f"Nguồn: {html.escape(item['source'])}\nPhát hiện: {stamp} (VN)\n"
                   "Giá niêm yết, chưa xác nhận tổng phí.\nTình trạng chi tiết: xem bài bán.")
        keyboard = {"inline_keyboard": [[{"text": "Mở bài bán trên Hatobox", "url": item["url"]}]]}
        base = {"chat_id": self.chat, "parse_mode": "HTML", "reply_markup": keyboard}
        if photos and item["image_url"]:
            try:
                return await self.call("sendPhoto", {**base, "photo": item["image_url"], "caption": caption})
            except TelegramError as exc:
                if exc.code != 400:
                    raise  # Never retry as text after an ambiguous network result.
        text = caption + f"\n<a href=\"{html.escape(item['url'], quote=True)}\">Link sản phẩm</a>"
        if item["image_url"]:
            text += f" · <a href=\"{html.escape(item['image_url'], quote=True)}\">Ảnh</a>"
        return await self.call("sendMessage", {**base, "text": text, "link_preview_options": {"is_disabled": True}})


class Hatobox:
    def __init__(self, context, config):
        self.context, self.config = context, config

    async def rows(self, page):
        timeout = self.config["page_timeout_seconds"] * 1000
        try:
            await page.wait_for_function("""() => {
              const main = document.querySelector('main');
              return document.querySelector('main .product-item .price-sub') ||
                (main && /0 kết quả/.test(main.innerText) && /Danh mục: .+|Từ khóa: .+/.test(main.innerText)) ||
                /verify (that )?you are human|checking your browser/i.test(document.body.innerText);
            }""", timeout=timeout)
        except Exception:
            text = await page.locator("body").inner_text(timeout=5000)
            if BLOCK_RE.search(text):
                raise SiteBlocked("Hatobox yêu cầu xác minh người dùng; bot đã dừng.") from None
            raise SourceError("Trang chưa tải xong hoặc cấu trúc đã đổi; không cập nhật mốc tin.") from None
        text = await page.locator("body").inner_text(timeout=5000)
        if BLOCK_RE.search(text):
            raise SiteBlocked("Hatobox yêu cầu xác minh người dùng; bot đã dừng.")
        main = await page.locator("main").inner_text(timeout=5000)
        if "Sản phẩm mới nhất" not in main:
            raise SourceError("Chưa xác minh được sắp xếp tin mới nhất.")
        for name in ("Còn hàng", "Mua ngay"):
            if not await page.get_by_role("radio", name=name, exact=True).is_checked():
                raise SourceError("Bộ lọc còn hàng / mua ngay không được giữ lại.")
        grids = page.locator("main .listing-grid")
        if await grids.count() != 1:
            raise SourceError("Danh sách sản phẩm không còn duy nhất; cần kiểm tra lại giao diện.")
        raw = await grids.locator(".product-item").evaluate_all(CARD_JS)
        if not raw and "0 kết quả" not in main:
            raise SourceError("Không tìm thấy thẻ sản phẩm; bot chưa cập nhật mốc tin.")
        result, ids = [], set()
        for row in raw:
            item = item_from_row(row)
            if item["id"] not in ids:
                result.append(item)
                ids.add(item["id"])
        return result

    async def scan(self, source, known):
        page = await self.context.new_page()
        observed, candidates, boundary, exhausted = [], [], False, False
        try:
            response = await page.goto(source["url"], wait_until="domcontentloaded",
                                       timeout=self.config["page_timeout_seconds"] * 1000)
            if response and response.status >= 400:
                text = await page.locator("body").inner_text(timeout=5000)
                if BLOCK_RE.search(text):
                    raise SiteBlocked("Nguồn đang yêu cầu xác minh người dùng; bot đã dừng.")
                raise SourceError(f"Nguồn trả mã HTTP {response.status}; sẽ chờ trước khi thử lại.")
            for _ in range(self.config["max_pages"]):
                rows = await self.rows(page)
                observed.extend(rows)
                for item in rows:
                    if item["id"] in known:
                        boundary = True
                        break
                    candidates.append(item)
                if boundary or not rows:
                    exhausted = not rows
                    break
                next_button = page.get_by_role("button", name="Tiếp theo", exact=True)
                if await next_button.count() == 0 or not await next_button.is_enabled():
                    exhausted = True
                    break
                first = rows[0]["url"]
                await next_button.click(timeout=10000)
                await page.wait_for_function("""first => {
                  const a = document.querySelector('main a.item-link-title');
                  return a && a.href !== first;
                }""", arg=first, timeout=self.config["page_timeout_seconds"]*1000)
            return observed, candidates, bool(known) and not boundary and not exhausted
        finally:
            await page.close()


async def run(config, db_path, once=False, dry_run=False):
    # Playwright is imported only for live polling, not for offline tests/status.
    from playwright.async_api import async_playwright
    token, chat = load_env() if not dry_run else ("", "")
    telegram = Telegram(token, chat)
    if not dry_run:
        await telegram.call("getMe")
        await telegram.call("getChat", {"chat_id": chat})
    store = Store(db_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    backoff_until, failures = {}, {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(locale="vi-VN", timezone_id="Asia/Ho_Chi_Minh")
        scraper = Hatobox(context, config)
        try:
            while not stop.is_set():
                started = time.monotonic()
                for source in config["sources"]:
                    if stop.is_set():
                        break
                    if time.monotonic() < backoff_until.get(source["key"], 0):
                        continue
                    try:
                        known = store.known(source["key"])
                        observed, candidates, gap = await scraper.scan(source, known)
                        if dry_run:
                            selected = [{**item, "group": group_for(item, config)} for item in observed if group_for(item, config)]
                            LOG.info("DRY RUN %s: %s thẻ, %s phù hợp; không lưu mốc, không gửi Telegram", source["name"], len(observed), len(selected))
                            print(json.dumps(selected[:10], ensure_ascii=False, indent=2))
                        else:
                            initialized, added = store.record(source, observed, candidates, config)
                            LOG.info("%s: %s thẻ, %s thông báo%s", source["name"], len(observed), added,
                                     " (đã tạo mốc, không gửi tin cũ)" if not initialized else "")
                            if gap:
                                LOG.warning("%s: chưa gặp mốc cũ trong giới hạn trang; có thể bỏ sót tin. Tăng max_pages hoặc dùng nguồn hẹp hơn.", source["name"])
                            await flush(store, telegram, config)
                        failures[source["key"]] = 0
                    except SiteBlocked:
                        if not dry_run:
                            try:
                                await telegram.text("⚠️ Hatobox đang yêu cầu xác minh người dùng. Bot đã dừng; cần kiểm tra truy cập trên VPS trước khi khởi động lại.")
                            except TelegramError:
                                pass
                        raise
                    except Exception as exc:
                        failures[source["key"]] = failures.get(source["key"], 0) + 1
                        delay = min(1800, 300 * 2 ** min(failures[source["key"]]-1, 3))
                        backoff_until[source["key"]] = time.monotonic() + delay
                        LOG.warning("%s: %s; chờ %ss", source["name"], str(exc) if isinstance(exc, SourceError) else type(exc).__name__, delay)
                        if not dry_run and failures[source["key"]] == 1:
                            try:
                                await telegram.text(f"⚠️ Chưa đọc được nguồn {source['name']}. Bot sẽ thử lại sau {delay}s; xem log VPS để kiểm tra.")
                            except TelegramError:
                                pass
                if not dry_run:
                    await flush(store, telegram, config)
                if once:
                    if failures and any(failures.values()):
                        raise SourceError("Có nguồn chưa đọc được; chưa xác minh vận hành.")
                    break
                elapsed = time.monotonic() - started
                LOG.info("Chu kỳ %.1fs; khoảng quét đặt %ss", elapsed, config["poll_seconds"])
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(1, config["poll_seconds"]-elapsed))
                except asyncio.TimeoutError:
                    pass
        finally:
            await context.close()
            await browser.close()
            store.db.close()


async def flush(store, telegram, config):
    for item in store.pending(config["notification_expiry_seconds"]):
        try:
            await telegram.notify(item, config.get("send_photos", True))
        except TelegramError as exc:
            LOG.warning("Telegram: %s; giữ thông báo trong hàng đợi", exc.code)
            # The client honors Telegram's retry_after across every subsequent call.
            break
        store.sent(item["id"])
        await asyncio.sleep(1.1)  # Per-chat pacing; no paid broadcasts.


def main():
    parser = argparse.ArgumentParser(description="Bot thông báo tin Hatobox mới qua Telegram")
    parser.add_argument("command", choices=["run", "once", "dry-run", "status", "test-telegram"])
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--db", default=str(ROOT / "state" / "watcher.sqlite3"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = read_config(args.config)
    if args.command == "test-telegram":
        token, chat = load_env()
        asyncio.run(Telegram(token, chat).text("✅ Kết nối Telegram của bot Hatobox thành công. Chế độ hiện tại: báo tin mới, chưa đánh giá giá rẻ."))
        return
    if args.command == "status":
        if not Path(args.db).exists():
            print("Chưa có dữ liệu vận hành. Chạy once hoặc run sau khi kết nối Telegram.")
        else:
            store = Store(args.db)
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            store.db.close()
        return
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(config, args.db, once=args.command in ("once", "dry-run"), dry_run=args.command == "dry-run"))


if __name__ == "__main__":
    try:
        main()
    except SiteBlocked as exc:
        LOG.error("%s", exc)
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] == "run" else 2)
    except (ValueError, SourceError, TelegramError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.error("Không khởi động/vận hành được: %s. Xem hướng dẫn và kiểm tra cài Chromium.", type(exc).__name__)
        sys.exit(1)
