# legal_inquiry.py
# 台灣法學三段論推演工作台 (Taiwan Civil-Law Syllogism Workbench)
# 結合「本地六階層法規 JSON 快取檢索」與「選配雲端 twlegalrag CLI 檢索」之雙軌架構

import os
import json
import time
import shutil
import subprocess
import gradio as gr
from google import genai

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from auth_engine import create_gradio_auth_app
from line_toolkit import send_line_message
from telegram_toolkit import send_telegram_message
from download_moj_laws import sync_moj_laws

# ==========================================
# 1. ⚙️ 全域環境變數與資料庫來源架構設定
# ==========================================
CLOUD_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
LAW_DB_ROOT = os.getenv("LAW_DB_ROOT", "./law_db").strip()
CASE_FILES_DIR = os.getenv("CASE_FILES_DIR", os.path.join(LAW_DB_ROOT, "案例事實")).strip()
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./outputs").strip()
BACKUP_DIR = os.getenv("BACKUP_DIR", "").strip()  # 選填：例如雲端硬碟同步資料夾路徑

# 定義六大法律階層來源、掃描路徑與遞迴規則（自動相對於 LAW_DB_ROOT）
DB_SOURCE_CONFIGS = [
    {
        "category": "憲法規範 (最高位階)",
        "path": os.path.join(LAW_DB_ROOT, "Constitution"),
        "recursive": True,
        "desc": "憲法、增修條文、大法官解釋與憲法法庭判決，位階最高"
    },
    {
        "category": "法律條文 (本法層級)",
        "path": LAW_DB_ROOT,
        "recursive": False,  # 僅讀取根目錄檔案，避免重複遞迴掃描子目錄
        "desc": "立法院三讀通過之實體法與程序法"
    },
    {
        "category": "法規命令與規則",
        "path": os.path.join(LAW_DB_ROOT, "Regulations"),
        "recursive": True,
        "desc": "辦法、命令、行政規則，不得牴觸法律與憲法"
    },
    {
        "category": "實務體系裁判與權威解析",
        "path": os.path.join(LAW_DB_ROOT, "Knowledge"),
        "recursive": True,
        "desc": "終審法院裁判見解、核心爭點與實務解析"
    },
    {
        "category": "主管機關專門函釋 (如衛福部/勞動部等)",
        "path": os.path.join(LAW_DB_ROOT, "MOHW_Interpretations"),
        "recursive": True,
        "desc": "行政法規與專門業務第一手主管機關函釋"
    },
    {
        "category": "立法理由與草案說明",
        "path": os.path.join(LAW_DB_ROOT, "Interpretations"),
        "recursive": True,
        "desc": "探求立法者原意之歷史與目的解釋輔助"
    }
]

# 存放所有結構化法規資料的記憶體快取
LAW_DB_CACHE: list[dict] = []


# ==========================================
# 📂 2. 多格式相容之「本地資料預載」模組
# ==========================================
def extract_document_info(data: dict, filename: str) -> tuple[str, str, str]:
    """從不同結構的 JSON（一般法規、裁判解析、函釋）萃取出標準化標題、路徑與檢索內文"""
    # 1. 體系化裁判與爭點解析格式
    if "裁判全文" in data or "主要爭點" in data or "所屬分類" in data:
        title = data.get("標題") or filename
        path = data.get("分類路徑") or data.get("所屬分類") or ""
        issues = data.get("主要爭點", "")
        fulltext = data.get("裁判全文", "")
        analysis = data.get("解析", "")
        laws = data.get("參考法條", "")
        keywords = data.get("關鍵詞", "")

        content_parts = []
        if path:
            content_parts.append(f"【分類路徑】：{path}")
        if issues:
            content_parts.append(f"【主要爭點】：\n{issues}")
        if laws:
            content_parts.append(f"【參考法條】：\n{laws}")
        if keywords:
            content_parts.append(f"【關鍵詞】：{keywords}")
        if analysis:
            content_parts.append(f"【專家解析】：\n{analysis[:2500]}")
        if fulltext:
            content_parts.append(f"【裁判要旨/全文精華】：\n{fulltext[:2000]}")

        full_content = "\n\n".join(content_parts)
        return title, path, full_content

    # 2. 一般法規 / 函釋 / 立法理由格式
    title = (
        data.get("title")
        or data.get("法規名稱")
        or data.get("名稱")
        or data.get("標題")
        or data.get("函釋字號")
        or filename
    )
    path = data.get("path") or data.get("分類路徑") or ""

    full_content = (
        data.get("full_content")
        or data.get("content")
        or data.get("內文")
        or data.get("條文內容")
        or data.get("主旨")
        or ""
    )

    # 若為陣列結構（如包含多條文的 JSON）
    if not full_content and "articles" in data:
        articles_text = []
        for art in data.get("articles", []):
            if isinstance(art, dict):
                art_no = art.get("article_no", art.get("條號", ""))
                art_text = art.get("article_content", art.get("條文", ""))
                articles_text.append(f"{art_no}\n{art_text}")
        full_content = "\n\n".join(articles_text)

    return title, path, full_content


def load_law_db_to_memory() -> dict:
    """在系統啟動或手動更新後，按規範位階將本地資料夾中的 JSON 讀入 RAM"""
    global LAW_DB_CACHE
    LAW_DB_CACHE = []

    print(f"\n[系統日誌] ⏳ 開始掃描本地法規庫 ({os.path.abspath(LAW_DB_ROOT)})...")
    start_time = time.time()

    total_stats = {cfg["category"]: 0 for cfg in DB_SOURCE_CONFIGS}

    for cfg in DB_SOURCE_CONFIGS:
        category = cfg["category"]
        folder = cfg["path"]
        is_recursive = cfg["recursive"]

        if not os.path.exists(folder):
            continue

        if is_recursive:
            file_generator = (
                os.path.join(root, file)
                for root, _, files in os.walk(folder)
                for file in files if file.endswith(".json")
            )
        else:
            file_generator = (
                os.path.join(folder, file)
                for file in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, file)) and file.endswith(".json")
            )

        for filepath in file_generator:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    filename = os.path.basename(filepath)
                    title, path, content = extract_document_info(data, filename)

                    if content.strip():
                        LAW_DB_CACHE.append({
                            "category": category,
                            "title": title,
                            "path": path,
                            "content": content
                        })
                        total_stats[category] += 1
            except Exception as e:
                print(f"⚠️ 略過無法解析的檔案 {filepath}: {e}")

    elapsed_time = time.time() - start_time
    print("\n" + "=" * 55)
    print(f"[系統日誌] ✅ 本地庫載入完成！共快取 {len(LAW_DB_CACHE)} 筆法規資料 (耗時: {elapsed_time:.2f} 秒)")
    for cat, count in total_stats.items():
        print(f"   📂 {cat}: {count} 筆")
    print("=" * 55 + "\n")
    return total_stats


def _format_db_status_md() -> str:
    """回傳目前記憶體快取筆數摘要（供 Gradio 介面顯示）"""
    count = len(LAW_DB_CACHE)
    if count <= 5:
        return (
            f"📊 **目前本地快取狀態**：僅載入 **{count}** 筆內建範例法規。"
            f"建議點擊下方按鈕一鍵下載法務部全國現行法規，或將自有 JSON 放入 `{LAW_DB_ROOT}`。"
        )
    return f"📊 **目前本地快取狀態**：已載入 **{count}** 筆結構化法規／實務資料（常駐 RAM 高速檢索中）。"


def sync_moj_and_reload_ui(include_orders: bool):
    """Gradio 按鈕事件：線上同步法務部開放資料並重新載入記憶體快取"""
    yield "⏳ 正在連線至法務部全國法規資料庫下載最新現行法規 ZIP 並拆檔歸類，請稍候約 5～15 秒..."
    try:
        stats = sync_moj_laws(law_db_root=LAW_DB_ROOT, include_orders=include_orders)
        load_law_db_to_memory()
        yield (
            f"✅ **同步與快取重載完成！** "
            f"本次寫入憲法 {stats['憲法']} 部、法律 {stats['法律']} 部、法規命令 {stats['命令']} 部。\n\n"
            f"{_format_db_status_md()}"
        )
    except Exception as e:
        yield f"❌ 同步失敗：{e}"


# ==========================================
# 🔍 3. 跨階層智慧段落檢索 ＋ 選配雲端檢索模組
# ==========================================
def _extract_relevant_excerpt(content: str, keywords: list[str], max_chars: int = 1800) -> str:
    """
    智慧段落擷取：
    - 若文件長度 <= max_chars（如一般函釋、單則裁判解析），完整保留全文。
    - 若為大型法典（如《民法》上千條文），依 `\n\n` 切分條文段落，優先挑出命中最多關鍵字的具體條文，
      並按原法典條號順序重組，避免固定切 `[:1800]` 導致只抓到第 1 條而漏掉真正爭點條文。
    """
    if len(content) <= max_chars:
        return content

    blocks = [b.strip() for b in content.split("\n\n") if b.strip()]
    if len(blocks) <= 1:
        return content[:max_chars]

    scored_blocks = []
    for idx, blk in enumerate(blocks):
        distinct_hits = sum(1 for k in keywords if k in blk)
        if distinct_hits > 0:
            total_hits = sum(blk.count(k) for k in keywords)
            scored_blocks.append((distinct_hits, total_hits, idx, blk))

    if not scored_blocks:
        return content[:max_chars]

    # 先按「命中不同關鍵字數 ➔ 總出現次數」挑出最精準的條文段落
    scored_blocks.sort(key=lambda x: (x[0], x[1]), reverse=True)

    selected = []
    used_chars = 0
    if blocks[0].startswith("【分類路徑】") and len(blocks[0]) < 200:
        selected.append((0, blocks[0]))
        used_chars += len(blocks[0]) + 2

    selected_indices = {idx for idx, _ in selected}
    for _, _, idx, blk in scored_blocks:
        if idx in selected_indices:
            continue
        if used_chars + len(blk) > max_chars and selected:
            continue
        selected.append((idx, blk))
        selected_indices.add(idx)
        used_chars += len(blk) + 4
        if used_chars >= max_chars:
            break

    selected.sort(key=lambda x: x[0])
    return "\n\n".join(blk for _, blk in selected)[:max_chars]


def search_local_law_db(keywords_str: str) -> str:
    """在 RAM 中高速掃描本地快取，依位階標註來源並精準擷取命中條文"""
    if not LAW_DB_CACHE:
        return "（尚未載入本地資料庫，或本地資料夾中無 JSON 檔案）"

    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    print(f"[系統日誌] ⚙️ 啟動本地法學檢索，關鍵字：【{', '.join(keywords)}】")

    retrieved_items = []
    for item in LAW_DB_CACHE:
        content = item["content"]
        title = item["title"]
        distinct_hits = sum(1 for k in keywords if (k in content or k in title))
        if distinct_hits > 0:
            title_hits = sum(1 for k in keywords if k in title)
            retrieved_items.append((distinct_hits, title_hits, item))

    retrieved_items.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if not retrieved_items:
        return "（本地資料庫查無直接命中之明文規定）"

    formatted_results = []
    for _, _, item in retrieved_items[:12]:
        header = f"【來源位階/類別：{item['category']}】\n【標題/案由】：{item['title']}"
        if item["path"]:
            header += f"\n【體系路徑】：{item['path']}"
        excerpt = _extract_relevant_excerpt(item["content"], keywords, max_chars=1800)
        formatted_results.append(f"{header}\n【命中條文/內文摘要】：\n{excerpt}")

    return "\n\n" + ("=" * 40 + "\n\n").join(formatted_results)


def search_tlr_cloud(keywords_str: str) -> str:
    """
    選配整合：呼叫外部安裝之 `twlegalrag` CLI 工具檢索雲端歷審判決與現行法規。
    支援兩種授權模式：
    1. 於 .env 設定 TWLEGALRAG_TLR_API_KEY
    2. 本機已完成 OAuth 登入（存在 ~/.twlegalrag/tlr_oauth.json）
    若本機未安裝 `twlegalrag` 或未設定授權，則自動安靜跳過。
    """
    enable_flag = os.getenv("ENABLE_TLR_CLOUD", "auto").strip().lower()
    if enable_flag in ("false", "0", "no"):
        return ""

    if not shutil.which("twlegalrag"):
        return ""

    tlr_api_key = os.getenv("TWLEGALRAG_TLR_API_KEY", "").strip()
    oauth_file = os.path.expanduser("~/.twlegalrag/tlr_oauth.json")

    if not tlr_api_key and not os.path.exists(oauth_file) and enable_flag != "true":
        return ""

    env = os.environ.copy()
    if tlr_api_key:
        env["TWLEGALRAG_TLR_API_KEY"] = tlr_api_key

    query = keywords_str.replace(",", " ").strip()
    top_n = os.getenv("TLR_TOP_N", "8").strip()

    try:
        res = subprocess.run(
            ["twlegalrag", "pack", query, "-n", top_n],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=25
        )
        if res.returncode == 0 and res.stdout.strip():
            return (
                "\n\n========================================\n\n"
                f"【🌐 外部雲端檢索結果 (twlegalrag CLI)】：\n{res.stdout.strip()}"
            )
    except Exception as e:
        print(f"⚠️ [twlegalrag] 雲端檢索略過或逾時: {e}")

    return ""


# ==========================================
# 🧠 4. 核心法學三段論推理引擎
# ==========================================
SYSTEM_INSTRUCTION = """你是一位極度嚴謹的台灣執業律師與法學研究者，具備深厚的大陸法系（歐陸法系）法理學養，精通承襲自德國法、日本法（並融合台灣本土實務、民商法與刑事訴訟程序演進）的台灣法學體系。

【絕對適用之規範階層與實務指引】：
在處理案件時，請嚴格按照下列規範位階與性質進行綜合涵攝：
1. 憲法規範（最高位階）：憲法條文、司法院大法官解釋及憲法法庭判決，具最高法律效力。
2. 法律條文（本法層級）：立法院通過之實體法與程序法。法律不得牴觸憲法。
3. 法規命令與規則：各部會依法律授權訂定之命令/辦法/標準，不得牴觸法律與憲法。
4. 終審法院裁判與實務解析：最高法院、最高行政法院之確定裁判見解與爭點梳理，為實務審理核心判斷標準。
5. 主管機關函釋：專門領域（如衛福部、勞動部、金管會等）之主管機關解釋，具行政實務參考拘束力。
6. 立法理由與草案：探求立法者原意之「歷史解釋」與「目的解釋」重要輔助，用以闡明法意，但不得逕行取代成文法明文。
7. 民事法理原則：有明文依明文、無明文依習慣（台灣）、無習慣依法理，允許依構成要件類似性審慎評估類推適用。
8. 刑事與行政法原則：嚴守罪刑法定與依法行政原則，對當事人有利及不利情形應一律注意，恪守客觀性義務。
9. 雙軌檢索資料融合原則（本地知識庫 ＋ 雲端最新法規裁判庫）：
   - 若同一法條或函釋於本地庫與雲端檢索庫版本不同，或雲端檢索標示該函釋已停止適用／裁判經上級審廢棄，請一律以最新修正條文與有效狀態為準。
   - 若所有檢索結果均無相關明文，嚴禁自行編造法條條號、判決字號或函釋字號！允許調用內建台灣法理知識進行論理，但必須明確標註：「（依內建台灣法理推論，非本次檢索明文）」。

【法系與術語隔離網（極度嚴格）】：
1. 排除英美法系預設：除台灣法律明文繼受者外（如消保法懲罰性賠償金等），嚴禁混用英美海洋法系（Common Law）概念（如陪審團裁決、衡平法獨立救濟等）。
2. 排除中國大陸法律用語：嚴禁使用「合同」（須用「契約」）、「知識產權」（須用「智慧財產權」）、「法人代表」（須用「代表人／法定代理人」）、「尋釁滋事」、「公訴人」、「抗辯權人」等非台灣法制詞彙。
3. 純淨語體：一律輸出嚴謹流暢之台灣繁體中文（zh-TW）法學意見書／判決書語體。

【法學三段論法輸出格式】：
請融合檢索內容，嚴格按照以下四大區塊格式輸出（不得省略任何標題）：

1. 【爭點整理】：
- 精準提煉出本案於實體法及程序法上的核心法律爭議。

2. 【大前提 (適用法規與裁判實務)】：
- 完整引述檢索庫中的相關憲法、法律、法規命令、主管機關函釋或法院裁判要旨。
- 必須明確逐項標註引用資料的【來源層級】與【法條／判決字號／函釋字號】。
  輸出範例：
  * [實務裁判] 最高法院○○年度台上字第○○號民事判決：...
  * [主管機關函釋] 衛署醫字第○○號函：...
  * [法律明文] 醫療法第63條第1項：...
- 若檢索庫查無明文而由內建法理推論，必須明確標註：「（依內建台灣法理推論，非本次檢索明文）」，絕不捏造字號。

3. 【小前提 (事實涵攝)】：
- 將當事人主張之具體事實，逐一對應並套入前揭法條構成要件與裁判爭點中。
- 進行細緻之構成要件該當性、違法性、有責性、因果關係或契約／侵權責任之論理推導。

4. 【結論與實務建議】：
- 基於上述三段論法涵攝，給出明確之法律評價結論、舉證責任分配提醒及後續程序建議。
"""


def save_and_notify_report(user_case: str, final_output: str, db_context: str):
    """將產出之法學意見書儲存至本地 outputs 資料夾，並依設定執行備份與推播"""
    line_payload = (
        f"📋 【諮詢案件事實】：\n{user_case.strip()}\n\n"
        f"================================\n\n"
        f"{final_output}"
    )

    full_file_with_appendix = (
        f"{line_payload}\n\n\n"
        f"==================================================\n"
        f"📚 【附錄：本次實際調取之本地與雲端檢索原始資料】\n"
        f"==================================================\n\n"
        f"{db_context}\n"
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"法務意見書_{int(time.time())}.md"
    md_filepath = os.path.join(OUTPUT_DIR, filename)

    with open(md_filepath, "w", encoding="utf-8-sig") as f:
        f.write(full_file_with_appendix)

    if BACKUP_DIR:
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            shutil.copy(md_filepath, os.path.join(BACKUP_DIR, filename))
            shutil.copy(md_filepath, os.path.join(BACKUP_DIR, filename.replace(".md", ".txt")))
        except Exception as e:
            print(f"⚠️ 備份至 BACKUP_DIR 失敗: {e}")

    send_line_message(line_payload)
    send_telegram_message(line_payload, channel="legal", filepath=md_filepath)


def run_inference(api_key: str, user_case: str):
    if not user_case.strip():
        yield "❌ 請先輸入具體案件事實內容。"
        return

    if not api_key:
        yield "❌ 找不到有效的 API Key，請重新整理畫面並登入！"
        return

    try:
        cloud_client = genai.Client(api_key=api_key)

        yield "🔍 【階段一】正在分析案情，提煉台灣法學專有名詞與跨階層爭點關鍵字..."

        kw_prompt = f"""請深入分析以下案件，萃取出 3 到 6 個最核心的台灣法律專有名詞、法條概念或實務爭點關鍵詞（例如：侵權行為, 醫療過失, 告知後同意, 損害賠償, 說明義務）。
請僅輸出以半形逗號「,」分隔的關鍵字列表，絕對不要輸出其他任何文字說明。

案件內容：
{user_case}"""

        kw_response = cloud_client.models.generate_content(
            model=CLOUD_MODEL,
            contents=kw_prompt,
            config={"temperature": 0.1}
        )
        keywords_str = (
            (kw_response.text or "")
            .strip()
            .replace("「", "")
            .replace("」", "")
            .replace("'", "")
            .replace("、", ",")
        )

        yield f"🔍 【階段一完成】鎖定核心關鍵字：**{keywords_str}**\n\n📂 【階段二】正在檢索本地法規庫與外部法學資料..."

        local_res = search_local_law_db(keywords_str)
        cloud_res = search_tlr_cloud(keywords_str)
        db_context = local_res + cloud_res

        source_summary = f"本地六階層法規庫（共 {len(LAW_DB_CACHE)} 筆快取）" + (
            " ＋ 雲端 twlegalrag 檢索" if cloud_res else ""
        )
        yield (
            f"🔍 【階段一完成】鎖定核心關鍵字：**{keywords_str}**\n"
            f"✅ 【階段二完成】已完成資料調取（來源：{source_summary}）。\n\n"
            f"⚖️ 【階段三】正進行歐陸法系三段論法涵攝，撰寫法律意見書..."
        )

        final_prompt = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            f"【檢索到的法規與實務見解】：\n{db_context}\n\n"
            f"【使用者案件事實】：\n{user_case}"
        )
        final_response = cloud_client.models.generate_content(
            model=CLOUD_MODEL,
            contents=final_prompt,
            config={"temperature": 0.0}
        )

        final_output = f"### 🔑 系統檢索關鍵字：【{keywords_str}】\n\n---\n\n{final_response.text}"
        yield final_output

        save_and_notify_report(user_case, final_output, db_context)

    except Exception as e:
        yield f"❌ 系統執行錯誤: {str(e)}"


def load_all_case_files() -> str:
    """一鍵讀取 CASE_FILES_DIR 底下所有的案例 JSON 檔案並合併"""
    if not os.path.exists(CASE_FILES_DIR):
        return f"⚠️ 找不到案例資料夾 ({CASE_FILES_DIR})，請確認路徑或建立範例 JSON 檔。"

    combined_text = ""
    file_count = 0

    for filename in sorted(os.listdir(CASE_FILES_DIR)):
        if filename.endswith(".json"):
            filepath = os.path.join(CASE_FILES_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    title = data.get("title", filename)
                    content = data.get("content", "")

                    if content:
                        file_count += 1
                        combined_text += f"【本地卷證 {file_count}：{title}】\n{content}\n\n{'=' * 40}\n\n"
            except Exception as e:
                print(f"讀取 {filename} 失敗: {e}")

    if file_count == 0:
        return f"⚠️ 在 {CASE_FILES_DIR} 中找不到任何有效的 JSON 案例檔。"

    return combined_text.strip()


# ==========================================
# 5. ✨ Gradio UI 介面
# ==========================================
def build_law_assistant_ui(api_key_state):
    gr.Markdown("# ⚖️ 台灣法學三段論推演工作台")
    gr.Markdown(
        "支援本地六階層法規 JSON 快取檢索（憲法、法律、命令、實務裁判、主管機關函釋、立法理由），"
        "並可選配串接外部 `twlegalrag` CLI 進行雲端判決與現行法條查核。\n"
        "> *免責聲明：本工具產出僅供法學研究與技術輔助參考，不構成正式法律意見或律師代理。*"
    )

    with gr.Accordion("📥 本地法規庫狀態與一鍵建置（法務部全國法規開放資料）", open=len(LAW_DB_CACHE) <= 5):
        db_status_md = gr.Markdown(_format_db_status_md())
        with gr.Row():
            include_orders_chk = gr.Checkbox(
                label="一併下載全國法規命令（約數千筆行政辦法，勾選後耗時較長）",
                value=False,
                scale=2
            )
            sync_moj_btn = gr.Button(
                "⬇️ 一鍵從法務部開放資料同步現行法規並重載快取",
                variant="secondary",
                size="sm",
                scale=2
            )

    with gr.Row():
        with gr.Column(scale=2):
            gr.Markdown("### 📝 第一步：輸入案件事實")
            load_case_btn = gr.Button("📂 一鍵匯入案例卷證資料 (JSON)", variant="secondary", size="sm")

            case_input = gr.Textbox(
                label="具體案例事實",
                lines=12,
                placeholder=(
                    "例如：某甲醫師於急診面對車禍大量內出血昏迷之病患乙，"
                    "因情況緊急無法即時取得家屬簽署書面手術同意書即行緊急開腹止血手術...\n\n"
                    "（亦可點擊上方按鈕自動載入案例資料夾內的範例卷證）"
                )
            )
            submit_btn = gr.Button("🔨 啟動法學三段論涵攝分析", variant="primary")

        with gr.Column(scale=3):
            gr.Markdown("### 📜 第二步：法學三段論推演結果")
            output_box = gr.Markdown(label="推演報告")

    sync_moj_btn.click(
        fn=sync_moj_and_reload_ui,
        inputs=[include_orders_chk],
        outputs=db_status_md
    )

    load_case_btn.click(
        fn=load_all_case_files,
        inputs=None,
        outputs=case_input
    )

    submit_btn.click(
        fn=run_inference,
        inputs=[api_key_state, case_input],
        outputs=output_box,
        scroll_to_output=True
    )


# ==========================================
# 🚀 啟動入口
# ==========================================
if __name__ == "__main__":
    load_law_db_to_memory()

    server_host = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1").strip()
    server_port = int(os.getenv("GRADIO_SERVER_PORT", "8085").strip())

    demo = create_gradio_auth_app(
        main_ui_function=build_law_assistant_ui,
        title="台灣法學三段論推演工作台"
    )
    demo.queue().launch(
        server_name=server_host,
        server_port=server_port,
        inbrowser=False,
        theme=gr.themes.Default(primary_hue="blue", secondary_hue="slate")
    )
