#!/usr/bin/env python3
"""umarengod 取得モジュール（騎手条件成績）。

カチウマ(umayomi/kachiuma)の jockey_db.py を複製・改良したもの。改良点:
  - 全列取得（1着/2着/3着/出走/勝率/連対率/複勝率/単複回収率）
  - 列位置を**ヘッダー文字列から判定**（見つからない時だけ既知の固定位置にフォールバック）
  - 取得失敗と「成績0」を区別する status を返す（失敗を空テーブルで握りつぶさない）
  - 障害戦は umarengod の条件が未確認のため取得せず unsupported を返す（芝の値で代用しない）

リーク防止: レース日 D に対し (D-3年)〜(D-1日) で集計する。当日は絶対に含めない。
"""
from __future__ import annotations
import datetime
import re
import time

URL_JOCKEY = "https://umarengod.com/etcsrch4.php"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TOP3_BASE = 0.25      # shrinkage の事前複勝率
K = 8                 # shrinkage の強さ
MIN_STARTS = 5        # 場×距離をそのまま信用する最低出走数（未満は全場へ fallback）
INTERVAL_SEC = 1.0    # umarengod へのリクエスト間隔（負荷をかけない）

# 既知のヘッダー（カチウマの診断で確定した実レスポンス）。ヘッダーが読めない時の最終手段。
FIXED_INDEX = {"name": 1, "wins": 2, "seconds": 3, "thirds": 4, "starts": 5,
               "win_rate": 6, "quinella_rate": 7, "place_rate": 8,
               "win_return_pct": 9, "place_return_pct": 10}


# ---------------------------------------------------------------- 期間
def period_3y(race_date: str) -> tuple[datetime.date, datetime.date]:
    """race_date(YYYYMMDD) → (開始日, 終了日)。終了日は D-1（当日を含めない）。"""
    d = datetime.datetime.strptime(race_date, "%Y%m%d").date()
    end = d - datetime.timedelta(days=1)
    try:
        start = d.replace(year=d.year - 3)
    except ValueError:              # 2/29 対策
        start = d.replace(year=d.year - 3, day=28)
    return start, end


def surface_param(surface: str | None) -> str | None:
    """出馬表の馬場種別 → umarengod の crs。障害は未確認なので None（取得しない）。"""
    if not surface:
        return None
    if "障" in surface:
        return None
    return "ダ" if "ダ" in surface else "芝"


def jockey_payload(surface: str, distance_m: int, race_date: str, place: str = "ALL") -> dict:
    s, e = period_3y(race_date)
    return {
        "fld": "kisyu", "go": "集　計",            # 「集」と「計」の間は全角スペース
        "yy1": str(s.year), "mm1": str(s.month), "dd1": str(s.day),
        "yy2": str(e.year), "mm2": str(e.month), "dd2": str(e.day),
        "place": place, "crs": surface_param(surface),
        "i1": str(distance_m), "i1_e": str(distance_m),   # 同値＝距離ピンポイント
        "course2": "ALL", "rc": "ALL", "grade": "ALL",
        "rec": "", "hn": "", "birthyear": "", "seni": "1",
        "val": "", "sort": "", "next": "",
    }


# ---------------------------------------------------------------- パース
def _norm_header(s: str) -> str:
    return re.sub(r"[\s　]+", "", s or "")


def _header_key(h: str) -> str | None:
    """ヘッダー文字列 → 項目キー。部分一致は誤爆しない順（長い語から）で判定する。
    例: 「複勝率」は「勝率」を含むので、先に複勝率を判定する。"""
    h = _norm_header(h)
    if not h:
        return None
    rules = [("複勝回収", "place_return_pct"), ("単勝回収", "win_return_pct"),
             ("複勝率", "place_rate"), ("連対率", "quinella_rate"), ("勝率", "win_rate"),
             ("出走", "starts"), ("1着", "wins"), ("2着", "seconds"), ("3着", "thirds")]
    for word, key in rules:
        if word in h:
            return key
    return None


def _to_int(s):
    m = re.search(r"\d+", (s or "").replace(",", ""))
    return int(m.group()) if m else None


def _to_float(s):
    m = re.search(r"\d*\.\d+|\d+", (s or "").replace(",", ""))
    return float(m.group()) if m else None


def _rate(s):
    """「.760」→0.76、「76.0%」→0.76。"""
    v = _to_float(s)
    if v is None:
        return None
    return v / 100.0 if v > 1.0 else v


def norm_name(name: str) -> str:
    """「Ｃ．ルメール」→「ルメール」、「▲小林美」→「小林美」、空白除去。"""
    n = re.sub(r"[\s　]+", "", name or "")
    n = n.replace("．", ".")
    n = n.lstrip("▲△☆★◇◎○")
    m = re.match(r"^[A-Za-zＡ-Ｚａ-ｚ]+\.(.+)$", n)
    return m.group(1) if m else n


def parse_stats_table(html: str, name_header: str = "騎手名") -> tuple[dict, dict]:
    """成績テーブル HTML → ({正規化名: 成績dict}, meta)。
    meta["table_found"] が False なら「取得失敗（ページ構造が想定外）」であり、成績0ではない。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    best, nrows = None, -1
    for t in soup.find_all("table"):
        txt = _norm_header(t.get_text())
        if name_header in txt and "出走" in txt and "複勝率" in txt:
            rows = t.find_all("tr")
            if len(rows) > nrows:
                best, nrows = t, len(rows)
    meta = {"table_found": best is not None, "column_mode": None, "rows": 0}
    out: dict = {}
    if best is None:
        return out, meta

    # ヘッダー行（td/th どちらでも）から列位置を決める
    cols = None
    for tr in best.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
        if any(_norm_header(c) == name_header for c in cells):
            cmap = {}
            for i, c in enumerate(cells):
                if _norm_header(c) == name_header:
                    cmap["name"] = i
                else:
                    k = _header_key(c)
                    if k and k not in cmap:
                        cmap[k] = i
            if "name" in cmap and "starts" in cmap and "place_rate" in cmap:
                cols = cmap
            break
    meta["column_mode"] = "header" if cols else "fixed_index"
    cols = cols or FIXED_INDEX
    need = max(cols.values())

    for tr in best.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(cells) <= need:
            continue
        raw_name = cells[cols["name"]]
        if not raw_name or _norm_header(raw_name) == name_header:
            continue
        starts = _to_int(cells[cols["starts"]])
        pr = _rate(cells[cols["place_rate"]])
        if starts is None or pr is None:
            continue
        rec = {"name_full": raw_name, "starts": starts, "place_rate": pr}
        for k in ("wins", "seconds", "thirds"):
            if k in cols:
                rec[k] = _to_int(cells[cols[k]])
        for k in ("win_rate", "quinella_rate"):
            if k in cols:
                rec[k] = _rate(cells[cols[k]])
        for k in ("win_return_pct", "place_return_pct"):
            if k in cols:
                rec[k] = _to_float(cells[cols[k]])
        out[norm_name(raw_name)] = rec
    meta["rows"] = len(out)
    return out, meta


# ---------------------------------------------------------------- 取得
def fetch_jockey_table(session, surface: str, distance_m: int, race_date: str,
                       place: str = "ALL") -> tuple[dict, dict]:
    """1条件ぶん POST。戻り (table, status)。status は必ず返す（失敗を黙って空にしない）。"""
    if surface_param(surface) is None:
        return {}, {"status": "unsupported", "reason": f"surface_not_supported:{surface}"}
    try:
        r = session.post(URL_JOCKEY, data=jockey_payload(surface, distance_m, race_date, place),
                         headers={"User-Agent": UA}, timeout=40)
    except Exception as e:  # noqa: BLE001
        return {}, {"status": "error", "reason": f"network:{type(e).__name__}"}
    finally:
        time.sleep(INTERVAL_SEC)
    if r.status_code != 200:
        return {}, {"status": "error", "reason": f"http_{r.status_code}"}
    r.encoding = r.apparent_encoding
    table, meta = parse_stats_table(r.text, "騎手名")
    if not meta["table_found"]:
        return {}, {"status": "error", "reason": "table_not_found"}
    return table, {"status": "ok", "rows": meta["rows"], "column_mode": meta["column_mode"]}


# ---------------------------------------------------------------- 照合・判定
def lookup(table: dict, jockey: str):
    """出馬表の省略名（川田）→ umarengod のフルネーム（川田将雅）を姓の前方一致で解決。"""
    key = norm_name(jockey)
    if not key or not table:
        return None
    if key in table:
        return table[key]
    cands = [v for k, v in table.items() if k.startswith(key) or key.startswith(k)]
    if len(cands) == 1:
        return cands[0]
    if cands:   # 複数該当（横山兄弟など）は出走数最多を採用
        return max(cands, key=lambda v: v["starts"])
    return None


def shrink(raw_place_rate: float, starts: int, base: float = TOP3_BASE, k: int = K) -> float:
    return (raw_place_rate * starts + base * k) / (starts + k)


def resolve_jockey(jockey_card: str, racecourse: str, surface: str, distance_m: int,
                   place_tbl: dict, place_st: dict, all_tbl: dict, all_st: dict) -> dict:
    """1頭ぶんの騎手ブロックを作る。status:
       ok / insufficient_sample(最終出走<5) / not_found(条件内出走なし or 名前照合失敗) /
       error(取得失敗) / unsupported(障害など未対応条件)"""
    base = {"name_card": jockey_card, "name": None,
            "primary_condition": {"racecourse": racecourse, "surface": surface,
                                  "distance": distance_m, "starts": None},
            "condition_used": None, "fallback_used": False, "fallback_reason": None}
    if place_st.get("status") == "unsupported":
        return {**base, "status": "unsupported", "reason": place_st.get("reason")}

    pc = lookup(place_tbl, jockey_card) if place_st.get("status") == "ok" else None
    ac = lookup(all_tbl, jockey_card) if all_st.get("status") == "ok" else None
    base["primary_condition"]["starts"] = (pc["starts"] if pc
                                           else (0 if place_st.get("status") == "ok" else None))

    chosen, used = None, None
    if pc and pc["starts"] >= MIN_STARTS:
        chosen, used = pc, racecourse
    elif ac:
        chosen, used = ac, "ALL"
        base["fallback_used"] = True
        if place_st.get("status") != "ok":
            base["fallback_reason"] = f"primary_fetch_{place_st.get('reason')}"
        elif pc:
            base["fallback_reason"] = f"primary_starts_lt_{MIN_STARTS}"
        else:
            base["fallback_reason"] = "primary_not_found"
    elif pc:   # 全場が取れず、場×距離の少数サンプルしか無い
        chosen, used = pc, racecourse

    if chosen is None:
        errs = [s for s in (place_st, all_st) if s.get("status") != "ok"]
        if errs:
            return {**base, "status": "error",
                    "reason": ";".join(str(s.get("reason")) for s in errs)}
        return {**base, "status": "not_found",
                "reason": "no_starts_in_condition_or_name_unmatched"}

    rec = {**base,
           "name": chosen["name_full"],
           "condition_used": {"racecourse": used, "surface": surface, "distance": distance_m},
           "starts": chosen["starts"],
           "wins": chosen.get("wins"), "seconds": chosen.get("seconds"),
           "thirds": chosen.get("thirds"),
           "win_rate": chosen.get("win_rate"), "quinella_rate": chosen.get("quinella_rate"),
           "raw_place_rate": round(chosen["place_rate"], 4),
           "adjusted_place_rate": round(shrink(chosen["place_rate"], chosen["starts"]), 4),
           "win_return_pct": chosen.get("win_return_pct"),
           "place_return_pct": chosen.get("place_return_pct")}
    rec["status"] = "insufficient_sample" if chosen["starts"] < MIN_STARTS else "ok"
    return rec
