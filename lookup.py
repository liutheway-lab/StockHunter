"""lookup.py — 從證交所／櫃買中心官方清單抓「中文名稱、官方產業別」，並支援用中文名稱或代號查詢。
沒有用到 streamlit，所以也可以單獨測試。
"""
import io
import re

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0"}
SOURCES = ((2, ".TW", "上市"), (4, ".TWO", "上櫃"))


def load_directory(timeout=30):
    """回傳 DataFrame：code, name, industry, suffix(.TW/.TWO), market(上市/上櫃)。
    資料來源：證交所 ISIN 公開清單（官方）。抓取過程的錯誤放在 df.attrs['errors']。"""
    rows, errors = [], []
    for mode, suffix, market in SOURCES:
        try:
            url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
            r = requests.get(url, timeout=timeout, headers=UA)
            r.encoding = "ms950"
            df = pd.read_html(io.StringIO(r.text))[0]
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            df = df[df["CFICode"] == "ESVUFR"]          # 只留一般股票
            for _, row in df.iterrows():
                parts = str(row["有價證券代號及名稱"]).split("\u3000")
                if len(parts) < 2 or not parts[0].isdigit():
                    continue
                ind = str(row.get("產業別", "")).strip()
                rows.append({
                    "code": parts[0],
                    "name": parts[1].strip(),
                    "industry": "" if ind == "nan" else ind,
                    "suffix": suffix,
                    "market": market,
                })
        except Exception as e:
            errors.append(f"{market}：{e}")
    if not rows:
        raise RuntimeError("無法取得股票清單：" + "；".join(errors))
    out = pd.DataFrame(rows).drop_duplicates("code")
    out.attrs["errors"] = errors
    return out


def resolve(query, directory, limit=8):
    """把使用者輸入（代號或中文名稱）轉成候選清單（list of dict）。
    - 輸入 4~6 碼數字：用代號精確比對
    - 輸入中文：先比對完全相同，再比對開頭相同，最後比對包含"""
    q = (query or "").strip().replace("\u3000", " ")
    if not q:
        return []
    q_code = re.sub(r"\.(TWO|TW)$", "", q.upper())
    if re.fullmatch(r"\d{4,6}", q_code):
        return directory[directory["code"] == q_code].to_dict("records")

    names = directory["name"]
    exact = directory[names == q]
    if len(exact):
        return exact.to_dict("records")[:limit]
    starts = directory[names.str.startswith(q)]
    contains = directory[names.str.contains(q, regex=False) & ~names.str.startswith(q)]
    return pd.concat([starts, contains]).head(limit).to_dict("records")


def load_themes(url, timeout=15):
    """讀取『自己維護』的題材表（例如 Google 試算表發佈成 CSV）。
    欄位：code, themes；themes 用頓號、逗號或斜線分隔。回傳 {代號: [題材, ...]}"""
    if not url:
        return {}
    df = pd.read_csv(url, dtype=str, storage_options={"User-Agent": "Mozilla/5.0"}) \
        if url.startswith("http") else pd.read_csv(url, dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]
    out = {}
    for _, row in df.iterrows():
        code = str(row.get("code", "")).strip()
        themes = str(row.get("themes", ""))
        if code and themes and themes != "nan":
            out[code] = [t.strip() for t in re.split(r"[、,，;；/]", themes) if t.strip()]
    return out
