"""飞书群消息接入：login / chats / messages / report。

依赖：python-dotenv；report 另需项目已有的 openai-agents。
授权和读取直接调用飞书 API。只有 report 会把读取结果发给 OpenAI。
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
API_ROOT = "https://open.feishu.cn/open-apis"
REDIRECT_URI = "http://127.0.0.1:3000/callback"
SCOPES = (
    "offline_access im:chat:read im:message:readonly "
    "im:message.group_msg:get_as_user"
)
MAX_TEXT_CHARS = 3000


class FeishuError(RuntimeError):
    pass


def config():
    load_dotenv(PROJECT_DIR / ".env", override=True, interpolate=False)
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id) or not app_secret:
        raise FeishuError("请在 .env 填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET。")
    return app_id, app_secret


def request_json(path, *, token=None, params=None, body=None):
    url = API_ROOT + path
    if params:
        url += "?" + urlencode(params)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
    )
    status = 200
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as error:
        status = error.code
        raw = error.read()
    except (URLError, TimeoutError) as error:
        raise FeishuError("连接飞书失败，请检查网络后重试。") from error
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise FeishuError(f"飞书返回非 JSON 响应，HTTP {status}。") from error
    if not isinstance(result, dict):
        raise FeishuError("飞书返回了无法识别的响应格式。")
    if status >= 400 or result.get("code", 0) not in (0, "0") or result.get("error"):
        code = result.get("code", result.get("error", status))
        detail = str(result.get("msg") or result.get("error_description") or "请求失败")
        # 只显示错误说明，不打印请求头、完整响应或凭证。
        for secret in [token, *(body or {}).values()]:
            if isinstance(secret, str) and len(secret) > 12:
                detail = detail.replace(secret, "[已隐藏]")
        raise FeishuError(f"飞书错误 {code}：{detail[:800]}")
    return result


def save_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        temp = Path(output.name)
        try:
            os.chmod(temp, 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def token_path(app_id):
    return PROJECT_DIR / "data" / f"feishu-token-{app_id}.json"


def store_token(app_id, result):
    if not result.get("access_token") or not result.get("expires_in"):
        raise FeishuError("授权响应缺少 access_token 或有效期，未保存。")
    saved = {
        "app_id": app_id,
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token"),
        "expires_at": time.time() + int(result["expires_in"]),
        "scope": result.get("scope", ""),
    }
    save_private_json(token_path(app_id), saved)
    return saved["access_token"]


def login():
    app_id, app_secret = config()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    callback = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 避免把回调里的授权码写入终端日志。

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            received_state = query.get("state", [""])[0]
            if url.path != "/callback":
                self.send_error(404)
                return
            if not secrets.compare_digest(received_state, state):
                self.send_error(400, "Invalid OAuth state")
                return
            if query.get("error"):
                callback["error"] = "你取消了授权，或飞书拒绝了这次授权。"
            elif query.get("code"):
                callback["code"] = query["code"][0]
            else:
                self.send_error(400, "Missing authorization code")
                return
            text = "授权回调已收到，请返回终端查看最终结果。".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

    url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
        "client_id": app_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    })
    try:
        server = HTTPServer(("127.0.0.1", 3000), CallbackHandler)
    except OSError as error:
        raise FeishuError("本机 3000 端口无法使用，请先关闭占用它的程序。") from error
    with server:
        server.timeout = 1
        print("请在浏览器完成飞书授权。若未自动打开，请打开：\n" + url, flush=True)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if callback.get("error"):
        raise FeishuError(callback["error"])
    if not callback.get("code"):
        raise FeishuError("等待授权超时，请重新运行 login。")
    result = request_json("/authen/v2/oauth/token", body={
        "grant_type": "authorization_code",
        "client_id": app_id,
        "client_secret": app_secret,
        "code": callback["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    store_token(app_id, result)
    print("飞书用户授权成功，凭证已保存到本机 data 目录。")
    if not result.get("refresh_token"):
        print("未获得刷新凭证；访问凭证到期后需要重新 login。")


def access_token():
    app_id, app_secret = config()
    path = token_path(app_id)
    if not path.exists():
        raise FeishuError("请先运行：python feishu_messages.py login")
    # Mac/Linux 文件锁：避免两个进程同时使用同一个一次性刷新凭证。
    import fcntl
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("app_id") != app_id:
            raise FeishuError("授权记录与应用不一致，请重新 login。")
        if saved.get("access_token") and time.time() < saved.get("expires_at", 0) - 120:
            return saved["access_token"]
        if not saved.get("refresh_token"):
            raise FeishuError("授权已到期，请重新运行 login。")
        try:
            result = request_json("/authen/v2/oauth/token", body={
                "grant_type": "refresh_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "refresh_token": saved["refresh_token"],
            })
        except FeishuError as error:
            raise FeishuError(f"刷新授权失败：{error}\n请重新运行 login。") from error
        return store_token(app_id, result)


def list_chats():
    token = access_token()
    params = {"page_size": 100, "sort_type": "ByCreateTimeAsc"}
    seen_pages, seen_chats = set(), set()
    while True:
        data = request_json("/im/v1/chats", token=token, params=params).get("data", {})
        for item in data.get("items", []):
            chat_id = item.get("chat_id", "")
            if chat_id and chat_id not in seen_chats:
                seen_chats.add(chat_id)
                name = " ".join(str(item.get("name", "未命名群")).splitlines())
                print(f"{name}  |  {chat_id}")
        if not data.get("has_more"):
            break
        next_page = data.get("page_token")
        if not next_page or next_page in seen_pages:
            raise FeishuError("群列表分页异常，以上列表可能不完整。")
        seen_pages.add(next_page)
        params["page_token"] = next_page
    print(f"共读取 {len(seen_chats)} 个可访问的群。此列表不包含私聊。")


def extract_text(item):
    if item.get("deleted"):
        return "[消息已撤回或删除]", False
    kind = item.get("msg_type", "unknown")
    if kind not in ("text", "post"):
        return f"[未解析的 {kind} 消息；图片、语音、附件和卡片内容未读取]", False
    raw = (item.get("body") or {}).get("content", "")
    try:
        content = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "[消息正文解析失败]", False
    if not isinstance(content, dict):
        return "[消息正文格式不支持]", False
    if kind == "text":
        text = str(content.get("text", ""))
    else:
        # 接收格式可能是单个富文本对象，也可能有语言层级。
        post = content
        if "content" not in post:
            post = next((content[k] for k in ("zh_cn", "en_us", "ja_jp")
                         if isinstance(content.get(k), dict)), {})
        lines = [str(post.get("title", ""))]
        for row in post.get("content", []):
            bits = []
            for node in row if isinstance(row, list) else []:
                if not isinstance(node, dict):
                    continue
                tag = node.get("tag")
                if tag in ("text", "a", "md"):
                    bits.append(str(node.get("text", "")))
                elif tag == "at":
                    bits.append("@" + str(node.get("user_name") or node.get("user_id", "未知用户")))
                else:
                    bits.append("[富文本中的非文字内容未解析]")
            lines.append("".join(bits))
        text = "\n".join(lines)
    # 用 API 返回的 mention 元数据替换占位符，不猜测人员姓名。
    for mention in item.get("mentions") or []:
        key, name = mention.get("key"), mention.get("name")
        if key and name:
            text = text.replace(key, "@" + name)
    text = text.strip()
    if not text:
        return "[没有可读取的文字正文]", False
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[正文已截断]"
    return text, True


def today_window():
    load_dotenv(PROJECT_DIR / ".env", override=True, interpolate=False)
    tz = ZoneInfo(os.getenv("REPORT_TIMEZONE") or "Europe/Copenhagen")
    now = datetime.now(tz).replace(microsecond=0)
    start = now.replace(hour=0, minute=0, second=0)
    return start, min(now, start + timedelta(days=1))


def api_items(path, params):
    params = dict(params)
    seen_pages = set()

    while True:
        time.sleep(0.15)
        response = request_json(path, token=access_token(), params=params)
        data = response.get("data")

        if not isinstance(data, dict):
            raise FeishuError("飞书返回的数据格式不正确。")

        yield from data.get("items", [])

        if not data.get("has_more"):
            return

        page = data.get("page_token")
        if not page or page in seen_pages:
            raise FeishuError("分页读取异常，本次数据可能不完整。")

        seen_pages.add(page)
        params["page_token"] = page


def read_today_messages(start=None, end=None):
    if start is None or end is None:
        start, end = today_window()

    messages, errors = [], []
    seen_chats, seen_messages = set(), set()

    if end <= start:
        return {"messages": [], "errors": [], "checked_groups": 0}

    try:
        chats = api_items("/im/v1/chats", {
            "page_size": 100,
            "sort_type": "ByCreateTimeAsc",
        })

        for chat in chats:
            chat_id = chat["chat_id"]
            if chat_id in seen_chats:
                continue

            seen_chats.add(chat_id)
            name = chat.get("name") or "未命名群"
            print(f"正在检查群：{name}", flush=True)

            try:
                items = api_items("/im/v1/messages", {
                    "container_id_type": "chat",
                    "container_id": chat_id,
                    "start_time": str(int(start.timestamp()) - 1),
                    "end_time": str(int(end.timestamp())),
                    "sort_type": "ByCreateTimeAsc",
                    "page_size": 50,
                })

                for item in items:
                    created_ms = int(item["create_time"])

                    if not start.timestamp() * 1000 <= created_ms < end.timestamp() * 1000:
                        continue

                    message_id = item["message_id"]
                    if message_id in seen_messages:
                        continue

                    text, has_text = extract_text(item)

                    messages.append({
                        "source": f"F{len(messages) + 1}",
                        "chat_id": chat_id,
                        "group": name,
                        "message_id": message_id,
                        "date": datetime.fromtimestamp(
                            created_ms / 1000, start.tzinfo
                        ).isoformat(),
                        "sender_id": (item.get("sender") or {}).get("id", "未知"),
                        "body": text,
                        "has_text": has_text,
                    })

                    seen_messages.add(message_id)

            except Exception as exc:
                errors.append(f"{name}（{chat_id}）读取不完整：{exc}")

    except Exception as exc:
        errors.append(f"群列表读取不完整：{exc}")

    return {
        "messages": messages,
        "errors": errors,
        "checked_groups": len(seen_chats),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        nargs="?",
        default="read",
        choices=["login", "read"],
    )
    args = parser.parse_args()

    if args.command == "login":
        login()
        return

    start, end = today_window()
    result = read_today_messages(start, end)
    result["start"] = start.isoformat()
    result["end"] = end.isoformat()
    result["scope"] = "群主消息中的文字；不包含私聊、话题回复和附件内容"

    path = PROJECT_DIR / "data" / f"FEISHU-TODAY-{start.date()}.json"
    save_private_json(path, result)

    print(
        f"\n检查了 {result['checked_groups']} 个群，"
        f"取得 {len(result['messages'])} 条消息。"
    )
    for error in result["errors"]:
        print("注意：" + error)

    print(f"读取结果已保存：{path}")


if __name__ == "__main__":
    try:
        main()
    except (FeishuError, OSError, ValueError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        sys.exit(1)