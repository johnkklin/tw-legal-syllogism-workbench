# auth_engine.py
# 統一安全驗證模組：支援 Streamlit 與 Gradio (iOS Face ID 自動登入 + 前端 DOM 殘留銷毀)
# 採用 Lazy Import 避免未安裝特定前端套件時引發錯誤

import os
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# 🛡️ 全局資安設定與金鑰驗證核心
# ==========================================

def _parse_allowed_lengths() -> tuple[int, ...]:
    """
    從環境變數讀取允許的 API Key 長度。
    預設同時支援：
    - 39 碼：Google Gemini 原生 API Key (AIzaSy...)
    - 53 碼：自建閘道或代理金鑰
    若設為 0 或空白則不限制特定長度（僅檢查最小長度 >= 20）。
    """
    raw = os.getenv("ALLOWED_KEY_LENGTHS", "39,53").strip()
    if not raw or raw == "0":
        return ()
    lengths = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            lengths.append(int(part))
    return tuple(lengths)

ALLOWED_KEY_LENGTHS = _parse_allowed_lengths()

# 嚴格字元白名單：僅允許標準 API Key 常見之可列印英數符號，防止 XSS 注入
KEY_PATTERN = re.compile(r"^(?!.*[<>&\"'`\\])[\x21-\x7E]+$")


def validate_api_key_format(raw_key: str) -> tuple[bool, str, str]:
    """
    嚴格校驗 API Key 格式與資訊熵（防堵重複字元暴力繞過介面）
    回傳: (是否通過, 清理後的Key, 錯誤訊息)
    """
    cleaned = (raw_key or "").strip()
    if not cleaned:
        return False, "", ""

    if ALLOWED_KEY_LENGTHS:
        if len(cleaned) not in ALLOWED_KEY_LENGTHS:
            expected_str = " 或 ".join(map(str, ALLOWED_KEY_LENGTHS))
            return False, "", f"❌ 金鑰長度不正確（目前 {len(cleaned)} 碼，需為 {expected_str} 碼）"
    elif len(cleaned) < 20:
        return False, "", "❌ 金鑰長度過短，請確認是否完整貼上"

    if not KEY_PATTERN.match(cleaned):
        return False, "", "❌ 金鑰包含非法字元"

    if len(set(cleaned)) < 8:
        return False, "", "❌ 金鑰複雜度異常，請輸入有效的 API Key"

    return True, cleaned, ""


def _js_length_condition() -> str:
    """動態產生前端自動送出的字數判斷條件"""
    if ALLOWED_KEY_LENGTHS:
        arr_str = "[" + ", ".join(map(str, ALLOWED_KEY_LENGTHS)) + "]"
        return f"{arr_str}.includes(currentVal.length)"
    return "currentVal.length >= 39"


# ==========================================
# 🟢 Streamlit 專區 (Face ID 自動登入 + 前端銷毀)
# ==========================================

def require_st_key():
    """Streamlit 安全驗證模組（支援 iOS Face ID 自動送出與前端殘留銷毀）"""
    import streamlit as st
    import streamlit.components.v1 as components

    if "api_key" not in st.session_state:
        st.session_state.api_key = ""

    def _verify_st_input():
        raw_val = st.session_state.get("_temp_st_api_key", "")
        is_valid, cleaned_key, err_msg = validate_api_key_format(raw_val)

        if is_valid:
            st.session_state.api_key = cleaned_key
            st.session_state._st_login_err = ""
            st.session_state._temp_st_api_key = ""
        elif raw_val.strip():
            st.session_state._st_login_err = err_msg
            st.session_state._temp_st_api_key = ""
        else:
            st.session_state._st_login_err = "❌ 請重新輸入。"

    if not st.session_state.api_key:
        st.info("🔒 請輸入您的 Gemini API Key（僅暫存於當次連線記憶體，不會寫入伺服器硬碟）。")

        st.text_input(
            "API Key：",
            type="password",
            key="_temp_st_api_key",
            on_change=_verify_st_input,
            placeholder="請點此刷臉帶入或貼上 API Key..."
        )

        if st.button("確認啟用", type="primary"):
            _verify_st_input()
            if st.session_state.api_key:
                st.rerun()

        if st.session_state.get("_st_login_err"):
            st.error(st.session_state._st_login_err)

        js_cond = _js_length_condition()
        components.html(
            f"""
            <script>
            (function() {{
                const parentDoc = window.parent.document;
                let triggered = false;

                if (!parentDoc.getElementById('st-ios-hidden-user')) {{
                    const hiddenUser = parentDoc.createElement('input');
                    hiddenUser.type = 'text';
                    hiddenUser.name = 'username';
                    hiddenUser.id = 'st-ios-hidden-user';
                    hiddenUser.autocomplete = 'username';
                    hiddenUser.style.display = 'none';
                    parentDoc.body.appendChild(hiddenUser);
                }}

                setInterval(() => {{
                    const pwdInput = parentDoc.querySelector('input[type="password"]');
                    if (!pwdInput) return;

                    if (pwdInput.getAttribute('autocomplete') !== 'current-password') {{
                        pwdInput.setAttribute('autocomplete', 'current-password');
                    }}

                    const currentVal = pwdInput.value ? pwdInput.value.trim() : "";

                    if ({js_cond} && !triggered) {{
                        triggered = true;

                        const tracker = pwdInput._valueTracker;
                        if (tracker) {{
                            tracker.setValue('');
                        }}

                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.parent.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeInputValueSetter.call(pwdInput, currentVal);
                        pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        pwdInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

                        setTimeout(() => {{
                            pwdInput.dispatchEvent(new KeyboardEvent('keydown', {{
                                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
                            }}));
                            pwdInput.blur();

                            const buttons = Array.from(parentDoc.querySelectorAll('button'));
                            const submitBtn = buttons.find(el => el.innerText.includes('確認啟用'));
                            if (submitBtn) {{
                                submitBtn.click();
                            }}

                            setTimeout(() => {{
                                if (pwdInput) {{
                                    nativeInputValueSetter.call(pwdInput, '');
                                }}
                            }}, 300);
                        }}, 150);
                    }}

                    if (currentVal.length === 0) {{
                        triggered = false;
                    }}
                }}, 250);
            }})();
            </script>
            """,
            height=0,
            width=0,
        )

        st.stop()

    return st.session_state.api_key


def st_logout():
    """Streamlit 登出按鈕（清空狀態）"""
    import streamlit as st

    if st.button("登出並清除金鑰"):
        st.session_state.api_key = ""
        if "_temp_st_api_key" in st.session_state:
            del st.session_state["_temp_st_api_key"]
        st.session_state._st_login_err = ""
        st.rerun()


# ==========================================
# 🟠 Gradio 專區 (Face ID 自動登入 + 前端 DOM 銷毀)
# ==========================================

def check_gr_key(key_input):
    import gradio as gr
    is_valid, cleaned_key, err_msg = validate_api_key_format(key_input)

    if is_valid:
        return cleaned_key, "", gr.update(visible=False), gr.update(visible=True), ""
    elif (key_input or "").strip():
        return "", "", gr.update(visible=True), gr.update(visible=False), err_msg
    else:
        return "", "", gr.update(visible=True), gr.update(visible=False), ""


def check_gr_key_on_change(key_input):
    import gradio as gr
    is_valid, cleaned_key, _ = validate_api_key_format(key_input)

    if is_valid:
        return cleaned_key, "", gr.update(visible=False), gr.update(visible=True), ""
    else:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()


def gr_logout():
    """Gradio 登出邏輯"""
    import gradio as gr
    return "", "", gr.update(visible=True), gr.update(visible=False), ""


AUTO_ENTER_JS = f"""
() => {{
    let triggered = false;

    setInterval(() => {{
        const pwdInput = document.querySelector('#gr-api-key-box input[type="password"]') || document.querySelector('input[type="password"]');
        if (!pwdInput || pwdInput.offsetParent === null) return;

        if (pwdInput.getAttribute('autocomplete') !== 'current-password') {{
            pwdInput.setAttribute('autocomplete', 'current-password');
        }}

        const currentVal = pwdInput.value ? pwdInput.value.trim() : "";

        if ({_js_length_condition()} && !triggered) {{
            triggered = true;

            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeInputValueSetter.call(pwdInput, currentVal);

            pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            pwdInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

            setTimeout(() => {{
                const loginBtn = document.querySelector('#gr-auto-login-btn');
                if (loginBtn) {{
                    loginBtn.click();
                }} else {{
                    pwdInput.dispatchEvent(new KeyboardEvent('keydown', {{
                        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
                    }}));
                }}

                setTimeout(() => {{
                    if (pwdInput && pwdInput.value) {{
                        nativeInputValueSetter.call(pwdInput, '');
                        pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                }}, 350);
            }}, 150);
        }}

        if (currentVal.length === 0) {{
            triggered = false;
        }}
    }}, 250);
}}
"""


def create_gradio_auth_app(main_ui_function, title="AI 智慧法學系統"):
    """
    統一的 Gradio 登入外掛框架（含前端密碼殘留自動銷毀機制 + 繞過 SSE 佇列防卡死）。
    若環境變數預設了 GEMINI_API_KEY 且啟用 SKIP_WEB_AUTH=true，亦可選擇直接進入主畫面。
    """
    import gradio as gr

    default_env_key = os.getenv("GEMINI_API_KEY", "").strip()
    skip_auth = os.getenv("SKIP_WEB_AUTH", "false").lower() == "true" and bool(default_env_key)

    with gr.Blocks(title=title) as demo:
        api_key_state = gr.State(default_env_key if skip_auth else "")

        with gr.Column(visible=not skip_auth) as login_pane:
            gr.Markdown(
                "## 🔒 系統登入\n"
                "請輸入您的 **Google Gemini API Key**（金鑰僅保存於瀏覽器當次連線 Session，關閉即銷毀）。"
            )

            gr.HTML('<input type="text" name="username" autocomplete="username" style="display:none;" />')

            placeholder_hint = (
                f"請貼上 API Key（支援 {' / '.join(map(str, ALLOWED_KEY_LENGTHS))} 碼）..."
                if ALLOWED_KEY_LENGTHS else "請貼上您的 Gemini API Key..."
            )
            api_key_input = gr.Textbox(
                label="Gemini API Key",
                type="password",
                placeholder=placeholder_hint,
                autofocus=True,
                elem_id="gr-api-key-box"
            )
            login_btn = gr.Button("🚀 確認登入", elem_id="gr-auto-login-btn", variant="primary")
            login_msg = gr.Markdown("")

        with gr.Column(visible=skip_auth) as main_pane:
            logout_btn = gr.Button("🚪 登出並清除金鑰", variant="stop", size="sm")
            main_ui_function(api_key_state)

        demo.load(
            fn=None, inputs=None, outputs=None, js=AUTO_ENTER_JS,
            queue=False, show_progress="hidden"
        )

        login_btn.click(
            fn=check_gr_key, inputs=[api_key_input],
            outputs=[api_key_state, api_key_input, login_pane, main_pane, login_msg],
            queue=False, show_progress="hidden"
        )

        api_key_input.submit(
            fn=check_gr_key, inputs=[api_key_input],
            outputs=[api_key_state, api_key_input, login_pane, main_pane, login_msg],
            queue=False, show_progress="hidden"
        )

        api_key_input.change(
            fn=check_gr_key_on_change, inputs=[api_key_input],
            outputs=[api_key_state, api_key_input, login_pane, main_pane, login_msg],
            queue=False, show_progress="hidden"
        )

        logout_btn.click(
            fn=gr_logout, inputs=None,
            outputs=[api_key_state, api_key_input, login_pane, main_pane, login_msg],
            queue=False, show_progress="hidden"
        )

    return demo
