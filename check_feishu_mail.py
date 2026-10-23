from demo_report import read_today_mail
from feishu_message import today_window


def main():
    start, end = today_window()

    print(
        f"读取范围：{start.isoformat()} 至 {end.isoformat()}",
        flush=True,
    )

    messages, failed = read_today_mail(start, end)

    for message in messages:
        print(f"\n邮件 UID：{message['uid']}")
        print("收件时间：", message["received_at"])
        print("发件人：", message["from"])
        print("主题：", message["subject"])

    print(
        f"\n本次测试：读到当天邮件 {len(messages)} 封，"
        f"读取失败 {len(failed)} 项。"
    )

    if failed:
        print(
            "读取不完整，相关 UID：",
            ", ".join(map(str, failed)),
        )
    elif not messages:
        print("邮箱查询成功，这段时间内没有邮件。")


if __name__ == "__main__":
    main()