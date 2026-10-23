import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import (
    HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)

STORE = (
    Path.home()
    / "Library/Application Support/AgentMessage"
    / "whatsapp-mcp/whatsapp-bridge/store"
)
MAX_BODY_CHARS = 3000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bridge_connected():
    try:
        token = (STORE / ".bridge-token").read_text().strip()
        if not token:
            return False

        request = Request(
            "http://127.0.0.1:8080/api/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        opener = build_opener(ProxyHandler({}), NoRedirect())

        with opener.open(request, timeout=3) as response:
            return json.load(response).get("connected") is True

    except Exception:
        return False


def read_today_whatsapp(start, end):
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("WhatsApp 读取需要有效、带时区的起止时间。")

    result = {"messages": [], "errors": []}
    db_path = STORE / "messages.db"

    if not db_path.is_file():
        result["errors"].append(
            "未找到 WhatsApp 数据库，请先启动同步程序并扫码。"
        )
        return result

    if not bridge_connected():
        result["errors"].append(
            "WhatsApp 同步连接未确认：以下仅为本地缓存，可能不完整。"
        )

    # 先宽范围筛选候选，再按带时区的时间精确筛选。
    first = (start.astimezone(timezone.utc) - timedelta(days=1)).date()
    last = (end.astimezone(timezone.utc) + timedelta(days=1)).date()

    sql = """
        SELECT m.id, m.chat_jid, m.sender, m.content,
               m.timestamp, m.is_from_me, m.media_type,
               c.name AS chat_name
        FROM messages AS m
        LEFT JOIN chats AS c ON c.jid = m.chat_jid
        WHERE m.deleted_at IS NULL
          AND (m.chat_jid LIKE '%@g.us'
               OR m.chat_jid LIKE '%@s.whatsapp.net'
               OR m.chat_jid LIKE '%@lid')
          AND substr(m.timestamp, 1, 10) BETWEEN ? AND ?
    """

    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"

        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, (first.isoformat(), last.isoformat()))

            for row in rows:
                try:
                    sent = datetime.fromisoformat(
                        str(row["timestamp"]).replace("Z", "+00:00")
                    )

                    # 同步程序须按上方命令使用 TZ=UTC 启动。
                    if sent.tzinfo is None:
                        sent = sent.replace(tzinfo=timezone.utc)

                    sent = sent.astimezone(start.tzinfo)

                except ValueError:
                    result["errors"].append(
                        f"WhatsApp 消息时间无法解析，已跳过：{row['id']}"
                    )
                    continue

                if not start <= sent < end:
                    continue

                body = (row["content"] or "").strip()
                media = row["media_type"] or ""
                notes = []

                if len(body) > MAX_BODY_CHARS:
                    notes.append(f"正文仅保留前 {MAX_BODY_CHARS} 字符。")

                if media:
                    notes.append("附件内容未解析，仅可使用已有文字或说明。")

                result["messages"].append({
                    "message_id": row["id"],
                    "chat_id": row["chat_jid"],
                    "chat": row["chat_name"] or row["chat_jid"],
                    "date": sent.isoformat(),
                    "sender": "我" if row["is_from_me"] else row["sender"],
                    "is_from_me": bool(row["is_from_me"]),
                    "body": body[:MAX_BODY_CHARS] or "[非文本消息，未解析]",
                    "has_text": bool(body),
                    "media_type": media,
                    "note": " ".join(notes),
                })

    except sqlite3.Error as exc:
        result["errors"].append(f"WhatsApp 本地数据库读取失败：{exc}")

    result["messages"].sort(
        key=lambda m: datetime.fromisoformat(m["date"]).timestamp()
    )

    for i, message in enumerate(result["messages"], 1):
        message["source"] = f"W{i}"

    return result


if __name__ == "__main__":
    from feishu_message import today_window

    start, end = today_window()
    result = read_today_whatsapp(start, end)

    print(f"范围：{start.isoformat()} 至 {end.isoformat()}")
    print(f"读到本地已同步的 WhatsApp 消息 {len(result['messages'])} 条。")

    for message in result["messages"][-5:]:
        print(
            f"[{message['source']}] {message['date']} "
            f"{message['chat']} | {message['sender']}"
        )
        print(message["body"][:200])

    for error in result["errors"]:
        print("提示：", error)