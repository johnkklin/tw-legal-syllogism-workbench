# download_moj_laws.py
# 法務部全國法規資料庫（政府資料開放平臺）一鍵同步與結構化歸檔工具
# 依據《著作權法》第9條與政府資料開放授權條款，合法下載現行憲法、法律與選配法規命令，
# 並自動拆分為本系統六階層記憶體快取相容之個別 JSON 檔案。

import os
import re
import io
import time
import json
import argparse
import zipfile
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MOJ_LAW_ZIP_URL = "https://law.moj.gov.tw/api/Ch/Law/JSON"      # 全國現行憲法與法律 (每日更新)
MOJ_ORDER_ZIP_URL = "https://law.moj.gov.tw/api/Ch/Order/JSON"  # 全國現行法規命令 (選配，檔案較大)


def _safe_filename(name: str) -> str:
    """移除作業系統不允許的檔名字元"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return cleaned[:120] or "unnamed_law"


def _fetch_and_parse_moj_zip(url: str, label: str, retries: int = 2) -> list[dict]:
    """從法務部開放資料 API 下載 ZIP（含自動重試）並於記憶體內以 utf-8-sig 安全解碼 JSON"""
    print(f"🚀 正在自法務部全國法規資料庫下載【{label}】({url})...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TW-Legal-Syllogism-Workbench/1.0"
    }

    last_err = None
    resp = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=90)
            resp.raise_for_status()
            break
        except Exception as exc:
            last_err = exc
            if attempt <= retries:
                print(f"⏳ 【{label}】連線異常或伺服器忙碌 (第 {attempt}/{retries + 1} 次)，3 秒後重試：{exc}")
                time.sleep(3)
            else:
                raise last_err

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not json_names:
            raise ValueError(f"壓縮包內找不到 JSON 檔案：{zf.namelist()}")
        raw_bytes = zf.read(json_names[0])
        # 法務部 ChLaw.json / ChOrder.json 帶有 UTF-8 BOM，必須使用 utf-8-sig 解碼
        payload = json.loads(raw_bytes.decode("utf-8-sig"))

    laws = payload.get("Laws") or payload.get("Orders") or []
    update_date = payload.get("UpdateDate", "未知日期")
    print(f"✅ 【{label}】下載解析成功！官方更新日期：{update_date}，原始筆數：{len(laws)}")
    return laws


def sync_moj_laws(law_db_root: str = None, include_orders: bool = False) -> dict:
    """
    下載法務部開放資料並依規範位階（憲法 / 法律 / 命令）自動拆分寫入本地資料夾。
    - 自動略過已廢止（LawAbandonNote 非空）之法規，確保 AI 僅引用現行有效條文。
    - 若選配之「法規命令」端點因法務部伺服器暫時 500 異常而失敗，將保留已下載之憲法與法律並優雅降級，不中斷整體流程。
    """
    target_root = (law_db_root or os.getenv("LAW_DB_ROOT", "./law_db")).strip()
    constitution_dir = os.path.join(target_root, "Constitution")
    regulations_dir = os.path.join(target_root, "Regulations")

    os.makedirs(target_root, exist_ok=True)
    os.makedirs(constitution_dir, exist_ok=True)
    os.makedirs(regulations_dir, exist_ok=True)

    datasets = [(MOJ_LAW_ZIP_URL, "現行憲法與法律", True)]
    if include_orders:
        datasets.append((MOJ_ORDER_ZIP_URL, "現行法規命令", False))

    stats = {
        "憲法": 0,
        "法律": 0,
        "命令": 0,
        "略過廢止或空檔": 0,
        "warnings": [],
    }

    for url, label, is_required in datasets:
        try:
            laws_list = _fetch_and_parse_moj_zip(url, label)
        except Exception as exc:
            if is_required:
                raise
            warn_msg = f"選配項目【{label}】暫時無法下載（法務部伺服器回應異常：{exc}），已略過並保留憲法與法律。"
            print(f"⚠️ {warn_msg}")
            stats["warnings"].append(warn_msg)
            continue

        for law in laws_list:
            # 略過已廢止或停止適用之舊法規
            if (law.get("LawAbandonNote") or "").strip():
                stats["略過廢止或空檔"] += 1
                continue

            law_name = (law.get("LawName") or "").strip()
            if not law_name:
                stats["略過廢止或空檔"] += 1
                continue

            law_level = (law.get("LawLevel") or "法律").strip()
            law_category = (law.get("LawCategory") or "").strip()
            modified_date = (law.get("LawModifiedDate") or "").strip()

            articles = []
            for art in law.get("LawArticles", []):
                art_type = (art.get("ArticleType") or "A").strip()
                art_no = (art.get("ArticleNo") or "").strip()
                art_content = (art.get("ArticleContent") or "").strip()

                if not art_content:
                    continue
                # 略過純編章節標題 (ArticleType == "C")，保留實際條文以提升檢索精準度
                if art_type == "C" and not art_no:
                    continue

                full_art_no = f"{law_name} {art_no}".strip() if art_no else law_name
                articles.append({
                    "article_no": full_art_no,
                    "article_content": art_content
                })

            if not articles:
                stats["略過廢止或空檔"] += 1
                continue

            # 依台灣規範位階自動分發目錄
            if "憲法" in law_level:
                dest_folder = constitution_dir
                stats["憲法"] += 1
            elif "命令" in law_level:
                dest_folder = regulations_dir
                stats["命令"] += 1
            else:
                dest_folder = target_root
                stats["法律"] += 1

            doc_obj = {
                "title": law_name,
                "path": law_category,
                "level": law_level,
                "modified_date": modified_date,
                "articles": articles,
            }

            out_filename = f"moj_{_safe_filename(law_name)}.json"
            out_path = os.path.join(dest_folder, out_filename)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(doc_obj, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 55)
    print(f"🎉 全國法規本地化建置完成！儲存根目錄：{os.path.abspath(target_root)}")
    print(f"   🏛️ 憲法規範：{stats['憲法']} 部 (-> Constitution/)")
    print(f"   ⚖️ 現行法律：{stats['法律']} 部 (-> 根目錄)")
    print(f"   📜 法規命令：{stats['命令']} 部 (-> Regulations/)")
    print(f"   ⏭️ 已自動過濾廢止或無條文法規：{stats['略過廢止或空檔']} 筆")
    print("=" * 55 + "\n")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="從法務部全國法規資料庫開放 API 一鍵下載現行法規並轉為本地 RAG 格式"
    )
    parser.add_argument(
        "--with-orders",
        action="store_true",
        help="一併下載全國法規命令（約數千筆行政命令與辦法；預設僅下載憲法與法律）"
    )
    parser.add_argument(
        "--dest",
        type=str,
        default=None,
        help="自訂輸出資料夾路徑（預設讀取 .env 的 LAW_DB_ROOT 或 ./law_db）"
    )
    args = parser.parse_args()

    try:
        sync_moj_laws(law_db_root=args.dest, include_orders=args.with_orders)
    except Exception as exc:
        print(f"❌ 法規同步失敗：{exc}")
