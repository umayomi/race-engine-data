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
import unicodedata

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


APPRENTICE = "▲△☆★◇◎○※"


def split_name(name: str) -> tuple[str | None, str]:
    """騎手名 → (イニシャル, 核)。外国人騎手のイニシャルは捨てずに保持する。
      「Ｃ．ルメール」→("C","ルメール") / 「Ｍデムーロ」→("M","デムーロ")（ドット無しも可）
      「▲小林美」→(None,"小林美") / 「鮫島駿」→(None,"鮫島駿")
    イニシャルを捨てると Ｍ．デムーロ と Ｃ．デムーロ が同一キーに衝突して別人を返す事故になる。"""
    n = unicodedata.normalize("NFKC", name or "")      # 全角英字・全角ピリオドを半角へ
    n = re.sub(r"[\s.・･]+", "", n).lstrip(APPRENTICE)
    m = re.match(r"^([A-Za-z])(.+)$", n)               # 日本人名は英字で始まらない
    if m:
        return m.group(1).upper(), m.group(2)
    return None, n


def norm_name(name: str) -> str:
    """後方互換: 核のみ返す。"""
    return split_name(name)[1]


def _is_subseq(short: str, long: str) -> bool:
    """「鮫島駿」⊂「鮫島克駿」のような 姓+名末字 の略称に対応（順序を保った部分列）。"""
    if len(short) < 2 or not long.startswith(short[0]):
        return False
    it = iter(long)
    return all(ch in it for ch in short)


def parse_stats_table(html: str, name_header: str = "騎手名") -> tuple[dict, dict]:
    """成績テーブル HTML → ({正規化名: 成績dict}, meta)。
    meta["table_found"] が False なら「取得失敗（ページ構造が想定外）」であり、成績0ではない。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    # umarengod は「検索フォームを含む外側テーブル」の中に「成績の内側テーブル」がある入れ子構造。
    # 単純に行数最大を選ぶと外側を掴み、フォーム見出しと結合したヘッダー行を読んで列がずれる。
    # → 内側（入れ子テーブルを持たない）を優先し、その中で行数最大を選ぶ。
    cands = []
    for t in soup.find_all("table"):
        txt = _norm_header(t.get_text())
        if name_header in txt and "出走" in txt and "複勝率" in txt:
            cands.append((0 if t.find("table") is None else 1, -len(t.find_all("tr")), t))
    cands.sort(key=lambda x: (x[0], x[1]))
    best = cands[0][2] if cands else None
    meta = {"table_found": best is not None, "column_mode": None, "rows": 0, "validated": None}
    out: dict = {}
    if best is None:
        return out, meta

    def _cmap_from(tr) -> dict | None:
        cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
        idx = [i for i, c in enumerate(cells) if _norm_header(c) == name_header]
        # 名前列が先頭付近にない行は、フォーム見出し等と結合した行なので採用しない
        if not idx or idx[0] > 3:
            return None
        cmap = {"name": idx[0]}
        for i, c in enumerate(cells):
            if i == idx[0]:
                continue
            k = _header_key(c)
            if k and k not in cmap:
                cmap[k] = i
        return cmap if {"starts", "place_rate"} <= set(cmap) else None

    def _valid(cols: dict) -> bool:
        """データ行に当ててみて筋が通るか検算（名前が数字だけ、複勝率が範囲外などを弾く）。"""
        need = max(cols.values())
        for tr in best.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cells) <= need:
                continue
            nm = cells[cols["name"]]
            if not nm or _norm_header(nm) == name_header:
                continue
            st, pr = _to_int(cells[cols["starts"]]), _rate(cells[cols["place_rate"]])
            if re.fullmatch(r"[\d.,%％\-]+", nm) or st is None or pr is None or not 0.0 <= pr <= 1.0:
                return False
            return True
        return False

    cols = None
    for tr in best.find_all("tr"):
        c = _cmap_from(tr)
        if c and _valid(c):
            cols, meta["column_mode"], meta["validated"] = c, "header", True
            break
    if cols is None:                      # ヘッダーが読めない/検算に落ちたら既知の固定位置
        cols = FIXED_INDEX
        meta["column_mode"] = "fixed_index"
        meta["validated"] = _valid(cols)
    need = max(cols.values())

    for tr in best.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(cells) <= need:
            continue
        raw_name = cells[cols["name"]]
        if not raw_name or _norm_header(raw_name) == name_header:
            continue
        starts = _to_int(cells[cols["starts"]])
        if starts is None:
            continue                       # 出走数が読めない行＝データ行でない
        rec = {"name_full": raw_name, "starts": starts}
        for k in ("wins", "seconds", "thirds"):
            if k in cols:
                rec[k] = _to_int(cells[cols[k]])
        for k in ("win_rate", "quinella_rate"):
            if k in cols:
                rec[k] = _rate(cells[cols[k]])
        for k in ("win_return_pct", "place_return_pct"):
            if k in cols:
                rec[k] = _to_float(cells[cols[k]])
        rec["place_rate"] = _rate(cells[cols["place_rate"]]) if "place_rate" in cols else None
        # 実仕様: 率や回収率が 0 の騎手は、そのセルが**空欄**になる（欠損ではなく 0）。
        # 例) 鮫島克駿 11戦 0-0-0 → 勝率/連対率/複勝率/回収率がすべて空欄。
        # 空欄行を捨てると「11戦して3着内0回」という有意な情報が消え、騎手が存在しない扱いに
        # なってしまうため、着度数から厳密に算出して補う。
        w, s2, s3, st = rec.get("wins"), rec.get("seconds"), rec.get("thirds"), starts
        if None not in (w, s2, s3) and st:
            if rec["place_rate"] is None:
                rec["place_rate"] = round((w + s2 + s3) / st, 3)
            if rec.get("win_rate") is None:
                rec["win_rate"] = round(w / st, 3)
            if rec.get("quinella_rate") is None:
                rec["quinella_rate"] = round((w + s2) / st, 3)
            if rec.get("win_return_pct") is None and w == 0:
                rec["win_return_pct"] = 0.0
            if rec.get("place_return_pct") is None and (w + s2 + s3) == 0:
                rec["place_return_pct"] = 0.0
        if rec["place_rate"] is None:
            continue                       # 着度数も率も読めない＝データ行でない
        ini, core = split_name(raw_name)
        rec["initial"] = ini
        out.setdefault(core, []).append(rec)
    meta["rows"] = sum(len(v) for v in out.values())
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
    """出馬表の省略名 → umarengod のフルネームを解決。戻り (rec, match_info) か None。
    段階: 完全一致 → 前方一致 → 部分列(姓+名末字の略称) の順。
    イニシャル付きの騎手は同じイニシャルのみ採用し、違えば「該当なし」にする（別人誤採用の防止）。"""
    ci, cc = split_name(jockey)
    if not cc or not table:
        return None
    tiers = [("exact", [r for k, v in table.items() if k == cc for r in v]),
             ("prefix", [r for k, v in table.items()
                         if k != cc and (k.startswith(cc) or cc.startswith(k)) for r in v]),
             ("subsequence", [r for k, v in table.items()
                              if k != cc and not (k.startswith(cc) or cc.startswith(k))
                              and _is_subseq(cc, k) for r in v])]
    for tier, cands in tiers:
        if not cands:
            continue
        if ci is not None:
            same = [r for r in cands if r.get("initial") == ci]
            if not same:
                continue          # イニシャル違い＝別人。次の段階へ（無ければ該当なし）
            cands = same
        best = max(cands, key=lambda r: r["starts"])
        return best, {"tier": tier, "candidates": len(cands),
                      "ambiguous": len(cands) > 1}
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

    p_hit = lookup(place_tbl, jockey_card) if place_st.get("status") == "ok" else None
    a_hit = lookup(all_tbl, jockey_card) if all_st.get("status") == "ok" else None
    pc, p_info = p_hit if p_hit else (None, None)
    ac, a_info = a_hit if a_hit else (None, None)
    base["primary_condition"]["starts"] = (pc["starts"] if pc
                                           else (0 if place_st.get("status") == "ok" else None))

    chosen, used, info = None, None, None
    if pc and pc["starts"] >= MIN_STARTS:
        chosen, used, info = pc, racecourse, p_info
    elif ac:
        chosen, used, info = ac, "ALL", a_info
        base["fallback_used"] = True
        if place_st.get("status") != "ok":
            base["fallback_reason"] = f"primary_fetch_{place_st.get('reason')}"
        elif pc:
            base["fallback_reason"] = f"primary_starts_lt_{MIN_STARTS}"
        else:
            base["fallback_reason"] = "primary_not_found"
    elif pc:   # 全場が取れず、場×距離の少数サンプルしか無い
        chosen, used, info = pc, racecourse, p_info

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
           "place_return_pct": chosen.get("place_return_pct"),
           "name_match": info}
    rec["status"] = "insufficient_sample" if chosen["starts"] < MIN_STARTS else "ok"
    return rec
