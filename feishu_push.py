import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent

load_dotenv(
    PROJECT_DIR / ".env",
    override=True,
    interpolate=False,
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_to_feishu(text):
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()

    if not webhook or not secret:
        raise RuntimeError(
            "请在 .env 填写 FEISHU_WEBHOOK_URL 和 FEISHU_WEBHOOK_SECRET。"
        )

    address = urlsplit(webhook)
    prefix = "/open-apis/bot/v2/hook/"

    if (
        address.scheme != "https"
        or address.netloc != "open.feishu.cn"
        or not address.path.startswith(prefix)
        or len(address.path) <= len(prefix)
        or address.query
        or address.fragment
    ):
        raise RuntimeError("请检查飞书自定义机器人的 Webhook 地址。")

    timestamp = str(int(time.time()))
    signing_key = f"{timestamp}\n{secret}".encode("utf-8")
    signature = base64.b64encode(
        hmac.new(signing_key, b"", hashlib.sha256).digest()
    ).decode("ascii")

    payload = {
        "timestamp": timestamp,
        "sign": signature,
        "msg_type": "text",
        "content": {"text": text},
    }

    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if len(encoded) > 20000:
        raise RuntimeError("推送内容超过飞书大小限制，请查看本机完整日报。")

    request = Request(
        webhook,
        data=encoded,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        opener = build_opener(NoRedirect())

        with opener.open(request, timeout=30) as response:
            result = json.load(response)

    except HTTPError as exc:
        raise RuntimeError(f"飞书推送请求返回 HTTP {exc.code}。") from None

    except (URLError, OSError):
        raise RuntimeError(
            "未能确认飞书是否收到请求，请先检查接收群，再决定是否重试。"
        ) from None

    except (ValueError, UnicodeError):
        raise RuntimeError("无法解析飞书接口返回的数据。") from None

    if not isinstance(result, dict):
        raise RuntimeError("飞书接口返回格式异常。")

    code = result.get("code", result.get("StatusCode"))

    if code != 0:
        message = result.get("msg") or result.get("StatusMessage") or "未知错误"
        raise RuntimeError(f"飞书推送失败：{code}，{message}")


if __name__ == "__main__":
    send_to_feishu("日报推送测试：看到这条消息，说明发送接口已连通。")
    print("测试消息已发送，请确认它出现在仅你本人接收的日报群。")