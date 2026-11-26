# telegram_toolkit.py
# 選配模組：支援將產出之法學意見書與檢索附錄自動推播至 Telegram Bot
# 未設定環境變數時自動略過，不影響主程式運作

import os
import time
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _get_telegram_config(channel: str = "legal") -> tuple[str, str]:
    """動態讀取環境變數中的 Chat ID 與對應頻道 Bot Token"""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    token_map = {
        "legal": os.getenv("TELEGRAM_BOT_TOKEN_LEGAL", "").strip(),
        "meeting": os.getenv("TELEGRAM_BOT_TOKEN_MEETING", "").strip(),
        "finance": os.getenv("TELEGRAM_BOT_TOKEN_FINANCE", "").strip(),
    }
    bot_token = token_map.get(channel) or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return chat_id, bot_token


def _split_text_smartly(text: str, max_len: int = 3800) -> list[str]:
    """智慧分段：優先在換行符號 (\n) 處切開，避免把句子或法條從正中間截斷。"""
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        split_idx = remaining.rfind("\n", 0, max_len)
        if split_idx == -1 or split_idx < max_len // 2:
            split_idx = remaining.rfind("。", 0, max_len)
            if split_idx != -1:
                split_idx += 1
            else:
                split_idx = max_len
        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _post_with_retry(url: str, retries: int = 3, **kwargs):
    """內建自動重試機制，防止網路偶發瞬斷或逾時"""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, **kwargs)
            if resp.status_code == 200:
                return resp
            print(f"⚠️ [telegram_toolkit] API 回應狀態碼 {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"⏳ [telegram_toolkit] 連線逾時或異常 (第 {attempt}/{retries} 次嘗試): {e}")
            if attempt < retries:
                time.sleep(2)
            else:
                raise e


def send_telegram_message(text: str, channel: str = "legal", filepath: str = None):
    """
    將文字（與選填的報告附件檔案）推播至指定的 Telegram Bot 頻道。
    若未於 .env 設定 Token 與 Chat ID，將安靜略過。
    """
    chat_id, bot_token = _get_telegram_config(channel)

    if not bot_token or not chat_id:
        return

    if text:
        try:
            msg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            chunks = _split_text_smartly(text, max_len=3800)
            total = len(chunks)
            for idx, chunk in enumerate(chunks, 1):
                prefix = f"📄 【第 {idx}/{total} 頁】\n\n" if total > 1 else ""
                _post_with_retry(
                    msg_url,
                    retries=3,
                    json={
                        "chat_id": chat_id,
                        "text": prefix + chunk
                    },
                    timeout=45
                )
                if total > 1 and idx < total:
                    time.sleep(0.5)
            print(f"✅ [telegram_toolkit] 文字訊息已透過 [{channel}] 推播完成（共 {total} 則）！")
        except Exception as e:
            print(f"❌ [telegram_toolkit] 文字推播發生錯誤: {str(e)}")

    if filepath and os.path.exists(filepath):
        try:
            doc_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
            for attempt in range(1, 4):
                try:
                    with open(filepath, "rb") as f:
                        resp = requests.post(
                            doc_url,
                            data={
                                "chat_id": chat_id,
                                "caption": f"📎 完整報告與檢索附錄：{os.path.basename(filepath)}"
                            },
                            files={"document": f},
                            timeout=60
                        )
                    if resp.status_code == 200:
                        print(f"✅ [telegram_toolkit] 附件檔案 [{os.path.basename(filepath)}] 已成功上傳至 Telegram！")
                        break
                    print(f"⚠️ [telegram_toolkit] 檔案上傳回應異常: {resp.text}")
                except Exception as e:
                    print(f"⏳ [telegram_toolkit] 檔案上傳逾時 (第 {attempt}/3 次嘗試): {e}")
                    if attempt < 3:
                        time.sleep(2)
                    else:
                        raise e
        except Exception as e:
            print(f"❌ [telegram_toolkit] 附件檔案上傳最終失敗: {str(e)}")
