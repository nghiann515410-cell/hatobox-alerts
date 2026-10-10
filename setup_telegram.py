#!/usr/bin/env python3
"""Enter a dedicated bot token locally, choose a verified private Telegram chat."""
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path
from watcher import ROOT, Telegram, TelegramError


def main():
    print("KẾT NỐI TELEGRAM — nhập token trực tiếp trên VPS, không gửi token qua chat.")
    print("1. Trong Telegram, mở @BotFather đã xác minh và dùng /newbot để tạo bot riêng.")
    print("2. Sao chép token; mở bot mới và gửi /start.")
    env_path = ROOT / ".env"
    if env_path.exists():
        if input("Đã có .env. Thay kết nối Telegram hiện tại? [y/N] ").lower() != "y":
            print("Giữ kết nối hiện tại.")
            return
    token = getpass.getpass("Token bot (ẩn khi nhập): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ValueError("Token không đúng định dạng.")
    client = Telegram(token)
    me = client.call_sync("getMe")
    if client.call_sync("getWebhookInfo").get("url"):
        raise ValueError("Bot đang có webhook. Hãy tạo bot riêng; trình cài không thay webhook của bot cũ.")
    print("Bot đã xác minh: @" + me["username"])
    input("Mở bot này, gửi /start từ Telegram của bạn, sau đó nhấn Enter... ")
    updates = client.call_sync("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    chats = {}
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") == "private" and message.get("text", "").split()[0:1] == ["/start"]:
            chats[chat["id"]] = chat
    if not chats:
        raise ValueError("Chưa thấy /start. Gửi /start đến đúng bot rồi chạy lại; dùng bot riêng mới tạo.")
    options = list(chats.values())
    for index, chat in enumerate(options, 1):
        print(f"{index}. {chat.get('first_name', '')} {chat.get('last_name', '')} — @{chat.get('username', '(không có username)')} — chat_id {chat['id']}")
    choice = input("Chọn số của tài khoản nhận thông báo: ").strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(options):
        raise ValueError("Số chọn không hợp lệ.")
    chat_id = str(options[int(choice)-1]["id"])
    client.chat = chat_id
    client.call_sync("sendMessage", {"chat_id": chat_id,
        "text": "✅ Bot Hatobox đã kết nối đúng tài khoản này. Chế độ: chỉ báo tin mới, chưa lọc giá rẻ. Sau lần tạo mốc đầu tiên, bot sẽ gửi tin mới phát hiện."})
    fd, temporary = tempfile.mkstemp(prefix=".telegram-", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, env_path)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            import pwd
            try:
                account = pwd.getpwnam("hatoboxbot")
                os.chown(env_path, account.pw_uid, account.pw_gid)
            except KeyError:
                pass
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    print("Đã lưu kết nối với quyền đọc hạn chế. Kiểm tra Telegram để thấy tin thử.")
    print("Bước tiếp: chạy dry-run, rồi once; chỉ bật dịch vụ sau khi cả hai kiểm tra thành công.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TelegramError, OSError) as exc:
        print(str(exc) if isinstance(exc, (ValueError, TelegramError)) else "Không ghi được .env; chạy với quyền phù hợp.", file=sys.stderr)
        sys.exit(1)
