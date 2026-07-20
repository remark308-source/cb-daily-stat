#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""


流程：
1. 呼叫 API（有分頁則逐頁抓取）取得所有可轉債的當日資料
2. 呼叫 TWSE（上市，CSV）/ TPEx（上櫃，JSON）官方每日收盤行情，取得全市場官方收盤價
3. 用官方收盤價「覆蓋」步驟1資料中的標的股價欄位（該欄位曾發現有明顯誤差，
   例如與官方股價相差 30~50% 的情況），並重新計算轉換價值與轉換溢價率
4. 過濾掉無效資料（CB市價為 0 或缺漏）
5. 與前一次存檔比對，若完全相同則判斷為「休市日/資料未更新」，跳過存檔
6. 計算 11 項統計指標
7. 儲存「當日明細快照」+ 更新「歷史彙總」
"""

import csv
import io
import json
import hashlib
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import pandas as pd

# ============ 設定 ============
API_URL = "https://cyclesinvest.com/convertible_bond_search.php"
API_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (compatible; CB-Stats-Bot/1.0)",
}

# 官方每日收盤行情來源（用來覆蓋/校正 CB 資料源的標的股價欄位）
TWSE_STOCK_DAY_ALL_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"  # 上市，回傳CSV
TPEX_DAILY_CLOSE_URL = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"  # 上櫃，回傳JSON

OFFICIAL_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DETAIL_DIR = DATA_DIR / "details"
HISTORY_CSV = DATA_DIR / "history.csv"
LAST_HASH_FILE = DATA_DIR / ".last_hash"

# 是否儲存每日明細 JSON（預設開啟）。
# 測試穩定後，在 GitHub Actions 的 repo Settings → Variables 加入
# SAVE_DETAIL_JSON = false 即可關閉，不需要修改程式碼。
import os
SAVE_DETAIL_JSON = os.environ.get("SAVE_DETAIL_JSON", "true").lower() != "false"

# 價格區間分組（與週更版保持一致）
PRICE_BINS = [0, 95, 100, 105, 110, 120, float("inf")]
PRICE_BIN_LABELS = ["<95", "95~100", "100~105", "105~110", "110~120", ">=120"]
PRICE_BIN_KEYS = ["lt95", "95_100", "100_105", "105_110", "110_120", "ge120"]

TW_TZ = timezone(timedelta(hours=8))


def fetch_raw_data() -> list[dict]:
    """呼叫 API，逐頁抓取所有可轉債資料（目前已知 pages=1，但保留分頁邏輯以防未來資料量變大）"""
    all_records = []
    page = 1
    while True:
        resp = requests.post(
            API_URL,
            data={"action": "search_convertible", "search_term": "", "category": "all", "page": page},
            headers=API_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        if not payload.get("success"):
            raise RuntimeError(f"API 回傳 success=false: {payload}")

        records = payload.get("data", [])
        all_records.extend(records)

        total_pages = int(payload.get("pages", 1) or 1)
        if page >= total_pages:
            break
        page += 1

    if not all_records:
        raise RuntimeError("API 回傳結果為空")

    return all_records


def _fetch_twse_prices() -> dict:
    """抓取 TWSE 上市股票每日收盤行情（CSV格式），回傳 {代號: 收盤價}"""
    price_map = {}
    resp = requests.get(TWSE_STOCK_DAY_ALL_URL, headers=OFFICIAL_API_HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    reader = csv.reader(io.StringIO(resp.text))
    header = None
    for row in reader:
        if not row:
            continue
        if header is None:
            header = row
            continue
        try:
            idx_code = header.index("證券代號")
            idx_close = header.index("收盤價")
        except ValueError:
            continue
        if len(row) <= max(idx_code, idx_close):
            continue
        code = row[idx_code].strip()
        close = to_float(row[idx_close])
        if code and close is not None and close > 0:
            price_map[code] = close
    return price_map


def _fetch_tpex_prices() -> dict:
    """抓取 TPEx 上櫃股票每日收盤行情（JSON格式，aaData陣列），回傳 {代號: 收盤價}"""
    price_map = {}
    resp = requests.get(
        TPEX_DAILY_CLOSE_URL,
        params={"l": "zh-tw", "o": "data"},
        headers=OFFICIAL_API_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("aaData", [])
    for row in rows:
        if len(row) < 3:
            continue
        code = str(row[0]).strip()
        close = to_float(row[2])  # 依既有社群範例，第3欄(index 2)為收盤價
        if code and close is not None and close > 0:
            price_map[code] = close
    return price_map


def fetch_official_stock_prices() -> dict:
    """
    分別呼叫 TWSE（上市）與 TPEx（上櫃）官方每日收盤行情，合併成
    {股票代號: 官方收盤價} 的對照表，用來校正 CB 資料源可能不準確的標的股價。

    任一來源呼叫失敗都不會中斷整體流程，只記錄警告；查不到的代號會在 clean_data 中
    fallback 使用 CB 資料源自帶的股價。
    """
    price_map = {}

    try:
        twse_prices = _fetch_twse_prices()
        price_map.update(twse_prices)
        print(f"[INFO] TWSE 上市官方收盤價取得 {len(twse_prices)} 檔")
    except Exception as e:
        print(f"[WARN] TWSE 官方股價抓取失敗，該部分將 fallback 用 CB 資料源自帶股價: {e}", file=sys.stderr)

    try:
        tpex_prices = _fetch_tpex_prices()
        # 代號重複時（理論上不會發生）以 TWSE 優先，不覆蓋
        for code, price in tpex_prices.items():
            price_map.setdefault(code, price)
        print(f"[INFO] TPEx 上櫃官方收盤價取得 {len(tpex_prices)} 檔")
    except Exception as e:
        print(f"[WARN] TPEx 官方股價抓取失敗，該部分將 fallback 用 CB 資料源自帶股價: {e}", file=sys.stderr)

    print(f"[INFO] 官方股價總計取得 {len(price_map)} 檔（上市+上櫃）")
    if not price_map:
        print("[WARN] 官方股價來源全部抓取失敗，本次將完全使用 CB 資料源自帶的標的股價（未經校正）",
              file=sys.stderr)

    return price_map


def to_float(value, default=None):
    """安全轉換字串為 float（去除千分位逗號），轉換失敗回傳 default"""
    if value is None:
        return default
    try:
        s = str(value).replace(",", "").replace("%", "").strip()
        if s == "" or s == "-":
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


def clean_data(raw: list[dict], official_prices: dict | None = None) -> pd.DataFrame:
    """
    整理原始資料成 DataFrame，並過濾無效標的。
    無效標的定義：CB市價 (bond_price) 為 0 或缺漏。

    標的股價校正：若 official_prices 有該標的股代號的官方收盤價，優先使用官方股價；
    找不到時 fallback 使用 CB 資料源自帶的 stock_price 欄位。
    轉換價值、轉換溢價率一律用「校正後」的股價重新計算，不採用資料源自帶的 conversion_value，
    確保跟官方股價口徑一致。
    """
    official_prices = official_prices or {}
    rows = []
    override_count = 0
    fallback_count = 0

    for item in raw:
        cb_price = to_float(item.get("bond_price"))
        conversion_price = to_float(item.get("conversion_price"))
        stock_code = item.get("stock_code")
        raw_stock_price = to_float(item.get("stock_price"))

        # 過濾：CB市價缺漏或為0，視為無效資料，不納入統計
        if cb_price is None or cb_price <= 0:
            continue

        # 標的股價校正：官方股價優先
        official_price = official_prices.get(stock_code)
        if official_price is not None:
            stock_price = official_price
            override_count += 1
        else:
            stock_price = raw_stock_price
            fallback_count += 1

        # 用校正後的股價重新計算轉換價值、轉換溢價率（不採用資料源自帶的 conversion_value）
        conversion_value = None
        premium_rate = None
        if conversion_price and conversion_price > 0 and stock_price is not None:
            conversion_value = stock_price / conversion_price * 100
            premium_rate = (cb_price / conversion_value - 1) * 100

        rows.append({
            "bond_code": item.get("bond_code"),
            "bond_name": item.get("bond_name"),
            "stock_code": stock_code,
            "cb_price": cb_price,
            "conversion_price": conversion_price,
            "stock_price": stock_price,
            "stock_price_source": "official" if official_price is not None else "cyclesinvest_fallback",
            "conversion_value": conversion_value,
            "premium_rate": premium_rate,
        })

    print(f"[INFO] 標的股價校正: 使用官方股價 {override_count} 檔，"
          f"找不到官方股價 fallback 用原始股價 {fallback_count} 檔")

    df = pd.DataFrame(rows)
    # 轉換價值/溢價率無法計算的（通常是轉換價格缺漏），一併排除
    df = df.dropna(subset=["conversion_value", "premium_rate"])
    return df


def compute_stats(df: pd.DataFrame, trade_date: str) -> dict:
    """計算11項統計指標（邏輯與週更版完全一致）"""
    if df.empty:
        raise RuntimeError("清理後的資料為空，無法計算統計指標")

    price_bins = pd.cut(df["cb_price"], bins=PRICE_BINS, labels=PRICE_BIN_LABELS, right=False)
    price_distribution = price_bins.value_counts().sort_index().to_dict()

    avg_cb_price = df["cb_price"].mean()
    pr75_price = df["cb_price"].quantile(0.75, interpolation="lower")
    pr90_price = df["cb_price"].quantile(0.90, interpolation="lower")
    avg_conversion_value = df["conversion_value"].mean()
    avg_premium_rate = df["premium_rate"].mean()

    count_cv_ge_100 = int((df["conversion_value"] >= 100).sum())
    count_cv_ge_120 = int((df["conversion_value"] >= 120).sum())
    count_premium_gt_0 = int((df["premium_rate"] > 0).sum())
    count_premium_ge_50 = int((df["premium_rate"] >= 50).sum())
    count_premium_ge_100 = int((df["premium_rate"] >= 100).sum())

    stats = {
        "date": trade_date,
        "total_count": len(df),
        "price_distribution": {str(k): int(v) for k, v in price_distribution.items()},
        "avg_cb_price": round(float(avg_cb_price), 2),
        "pr75_price": round(float(pr75_price), 2),
        "pr90_price": round(float(pr90_price), 2),
        "avg_conversion_value": round(float(avg_conversion_value), 2),
        "avg_premium_rate": round(float(avg_premium_rate), 2),
        "count_conversion_value_ge_100": count_cv_ge_100,
        "count_conversion_value_ge_120": count_cv_ge_120,
        "count_premium_rate_gt_0": count_premium_gt_0,
        "count_premium_rate_ge_50": count_premium_ge_50,
        "count_premium_rate_ge_100": count_premium_ge_100,
    }
    return stats


def compute_hash(raw: list[dict]) -> str:
    """對原始資料計算 hash，用來判斷今天資料是否跟昨天完全相同（可能是休市日/資料未更新）"""
    serialized = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def is_same_as_last_run(current_hash: str) -> bool:
    if not LAST_HASH_FILE.exists():
        return False
    last_hash = LAST_HASH_FILE.read_text(encoding="utf-8").strip()
    return last_hash == current_hash


def save_outputs(raw: list[dict], df: pd.DataFrame, stats: dict, trade_date: str, current_hash: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 存當日明細快照（可透過環境變數 SAVE_DETAIL_JSON=false 關閉）
    if SAVE_DETAIL_JSON:
        DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        detail_records = df.round(4).to_dict(orient="records")
        detail_output = {
            "date": trade_date,
            "total_raw": len(raw),
            "total_valid": len(df),
            "records": detail_records,
        }
        detail_path = DETAIL_DIR / f"{trade_date}.json"
        detail_path.write_text(
            json.dumps(detail_output, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"[OK] 已儲存明細快照: {detail_path}")
    else:
        print("[INFO] SAVE_DETAIL_JSON=false，略過明細快照儲存")

    flat_stats = {
        "date": stats["date"],
        "total_count": stats["total_count"],
        "avg_cb_price": stats["avg_cb_price"],
        "pr75_price": stats["pr75_price"],
        "pr90_price": stats["pr90_price"],
        "avg_conversion_value": stats["avg_conversion_value"],
        "avg_premium_rate": stats["avg_premium_rate"],
        "count_conversion_value_ge_100": stats["count_conversion_value_ge_100"],
        "count_conversion_value_ge_120": stats["count_conversion_value_ge_120"],
        "count_premium_rate_gt_0": stats["count_premium_rate_gt_0"],
        "count_premium_rate_ge_50": stats["count_premium_rate_ge_50"],
        "count_premium_rate_ge_100": stats["count_premium_rate_ge_100"],
    }
    for label, key in zip(PRICE_BIN_LABELS, PRICE_BIN_KEYS):
        flat_stats[f"price_bin_{key}"] = stats["price_distribution"].get(label, 0)

    new_row = pd.DataFrame([flat_stats])

    if HISTORY_CSV.exists():
        history_df = pd.read_csv(HISTORY_CSV)
        history_df = history_df[history_df["date"] != trade_date]
        history_df = pd.concat([history_df, new_row], ignore_index=True)
    else:
        history_df = new_row

    history_df = history_df.sort_values("date").reset_index(drop=True)
    history_df.to_csv(HISTORY_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] 已更新歷史彙總: {HISTORY_CSV}")

    LAST_HASH_FILE.write_text(current_hash, encoding="utf-8")


def main():
    trade_date = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    print(f"=== 開始執行可轉債每日統計（cyclesinvest 資料源） ({trade_date}) ===")

    try:
        raw = fetch_raw_data()
    except Exception as e:
        print(f"[ERROR] 抓取資料失敗: {e}", file=sys.stderr)
        sys.exit(1)

    current_hash = compute_hash(raw)

    if is_same_as_last_run(current_hash):
        print(f"[SKIP] 今天({trade_date})的資料跟上次執行完全相同，"
              f"判斷為休市日或資料尚未更新，不存檔。")
        sys.exit(0)

    print("[INFO] 開始抓取 TWSE 即時股價，用來校正標的股價...")
    print("[INFO] 開始抓取 TWSE / TPEx 官方每日收盤行情，用來校正標的股價...")
    official_prices = fetch_official_stock_prices()

    df = clean_data(raw, official_prices)
    print(f"[INFO] 原始筆數: {len(raw)}，過濾後有效筆數: {len(df)}")

    try:
        stats = compute_stats(df, trade_date)
    except Exception as e:
        print(f"[ERROR] 計算統計指標失敗: {e}", file=sys.stderr)
        sys.exit(1)

    print("[INFO] 統計結果:")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    save_outputs(raw, stats, trade_date, current_hash)
    print("=== 執行完成 ===")


if __name__ == "__main__":
    main()
