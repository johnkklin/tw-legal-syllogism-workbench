# line_toolkit.py
# 選配模組：支援將產出之法學意見書自動推播至 LINE Messaging API
# 未設定環境變數時自動略過，不影響主程式運作

import os
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _split_line_chunks(text: str, max_len: int = 4500) -> list[str]:
    """LINE 單則訊息上限 5000 字，安全分段切於 4500 字內"""
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        split_idx = remaining.rfind("\n", 0, max_len)
        if split_idx == -1 or split_idx < max_len // 2:
            split_idx = max_len
        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_line_message(text: str):
    """
    透過 LINE Messaging API Push Message 推播結果。
    若未設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_TARGET_USER_ID 則自動略過。
    """
    channel_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    target_id = os.getenv("LINE_TARGET_USER_ID", "").strip()

    if not channel_token or not target_id or not text:
        return

    chunks = _split_line_chunks(text, max_len=4500)
    messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {channel_token}"
            },
            json={
                "to": target_id,
                "messages": messages
            },
            timeout=30
        )
        if resp.status_code == 200:
            print(f"✅ [line_toolkit] 已成功推播至 LINE（共 {len(messages)} 則分段）！")
        else:
            print(f"⚠️ [line_toolkit] LINE API 回應異常 ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"❌ [line_toolkit] LINE 推播發生錯誤: {e}")
