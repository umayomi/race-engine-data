#!/usr/bin/env python3
"""
カチウマ — netkeiba 取得・解析の低レベル層（Phase 1）

役割:
  - find_race_ids(date)     : ある開催日(YYYYMMDD)の全 race_id を取得
  - parse_shutuba(...)       : 出馬表を構造化（馬番/馬名/性齢/騎手/オッズ/人気）
  - get_win_odds(race_id)    : 単勝オッズをAPIから取得（人気はオッズ順で算出）
  - parse_result(...)        : レース結果（着順/タイム/上がり）
  - parse_horse_pastruns(...): 各馬の過去走（直近N走）

注意（重要）:
  netkeiba の HTML/エンドポイントは変わることがある。各セレクタは「壊れる前提」で、
  取れなかった項目は None にして WARNING ログを出す（黙って失敗しない）。
  実HTMLでの初回検証が必要（ビルド環境からは netkeiba に到達できないため未検証）。
"""

from __future__ import annotations
import json
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("netkeiba")

BASE_RACE = "https://race.netkeiba.com"
BASE_DB = "https://db.netkeiba.com"

HEADERS = {
    # netkeibaは非ブラウザUAやiPhone UAだと簡易ページを返すため、PC版Chrome相当を名乗る
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
}
INTERVAL_SEC = 1.5  # アクセス間隔（負荷をかけない）

# 競馬場コード(race_idの5-6桁目) -> 名称
TRACK = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}

# 競馬場 -> 回り（コース適性の特徴量用）
DIRECTION = {
    "東京": "左", "中京": "左", "新潟": "左",
    "札幌": "右", "函館": "右", "福島": "右", "中山": "右",
    "京都": "右", "阪神": "右", "小倉": "右",
}


def get(url: str, retries: int = 3, timeout: int = 20) -> str:
    """負荷をかけないGET。リトライ・指数バックオフ・適切なエンコーディング。"""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            # db.netkeiba は EUC-JP、race.netkeiba は UTF-8
            r.encoding = "euc-jp" if "db.netkeiba.com" in url else "utf-8"
            time.sleep(INTERVAL_SEC)
            return r.text
        except Exception as e:  # noqa
            last = e
            log.warning("GET失敗(%d/%d) %s : %s", i + 1, retries, url, e)
            time.sleep(INTERVAL_SEC * (i + 1))
    raise last


# ----------------------------------------------------------------------
# レースID探索
# ----------------------------------------------------------------------
def find_race_ids(date: str) -> list[str]:
    """開催日(YYYYMMDD)の全 race_id(12桁) を返す。非開催日は空。"""
    url = f"{BASE_RACE}/top/race_list_sub.html?kaisai_date={date}"
    try:
        html = get(url)
    except Exception:
        log.error("レース一覧の取得に失敗: %s", date)
        return []
    ids = set(re.findall(r"race_id=(\d{12})", html))
    out = sorted(ids)
    log.info("%s: race_id %d件", date, len(out))
    return out


# ----------------------------------------------------------------------
# 出馬表
# ----------------------------------------------------------------------
# クラス(格)を数値化。新馬/未勝利=1 … オープン=5 … G3=6 G2=7 G1=8
def _parse_class(soup: BeautifulSoup, race_name: str | None):
    """RaceData02のspanからクラス条件を、レース名/アイコンからグレードを判定。
    戻り: (level:int|None, label:str|None)"""
    d2 = soup.select_one(".RaceData02")
    spans = [_text(sp) or "" for sp in d2.select("span")] if d2 else []
    blob = " ".join(spans)
    # 全角数字→半角（netkeibaは「１勝クラス」等の全角表記）
    blob = blob.replace("１", "1").replace("２", "2").replace("３", "3").replace("　", " ")
    level, label = None, None
    # 条件クラス（新しい「N勝クラス」と旧「N00万下」両対応）
    if "新馬" in blob:
        level, label = 1, "新馬"
    elif "未勝利" in blob:
        level, label = 1, "未勝利"
    elif re.search(r"1\s*勝", blob) or "500万" in blob:
        level, label = 2, "1勝クラス"
    elif re.search(r"2\s*勝", blob) or "1000万" in blob:
        level, label = 3, "2勝クラス"
    elif re.search(r"3\s*勝", blob) or "1600万" in blob:
        level, label = 4, "3勝クラス"
    elif "オープン" in blob or re.search(r"\bL\b", blob):
        level, label = 5, "オープン"
    # グレード（アイコン class か レース名の表記で上書き）
    rn = soup.select_one(".RaceName")
    icon = ""
    if rn:
        for el in rn.find_all(True):
            icon += " ".join(el.get("class", [])) + " "
    nm = race_name or ""
    if "Icon_GradeType1" in icon or re.search(r"[\(（]?G[ⅠI1][\)）]?", nm):
        level, label = 8, "G1"
    elif "Icon_GradeType2" in icon or re.search(r"[\(（]?G[ⅡII2][\)）]?", nm):
        level, label = 7, "G2"
    elif "Icon_GradeType3" in icon or re.search(r"[\(（]?G[ⅢIII3][\)）]?", nm):
        level, label = 6, "G3"
    return level, label


# 着差表記 → 馬身(おおよそ)。すぐ前の馬との差。Noneは勝ち馬/不明
_MARGIN_WORDS = {"同着": 0.0, "ハナ": 0.1, "アタマ": 0.2, "クビ": 0.3, "大差": 10.0}

def _margin_to_len(s: str | None):
    if not s:
        return None
    s = s.strip()
    for w, v in _MARGIN_WORDS.items():
        if w in s:
            return v
    # 例 "1.3/4"=1+3/4, "3/4"=0.75, "1/2"=0.5, "2"=2.0
    m = re.fullmatch(r"(?:(\d+)\.)?(\d+)/(\d+)", s)
    if m:
        whole = int(m.group(1)) if m.group(1) else 0
        return round(whole + int(m.group(2)) / int(m.group(3)), 3)
    m = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m:
        return float(s)
    return None


def _race_header(soup: BeautifulSoup, race_id: str) -> dict:
    track = TRACK.get(race_id[4:6], "")
    race_no = int(race_id[-2:])
    name = _text(soup.select_one(".RaceName")) or _text(soup.select_one(".RaceList_Item02 .RaceName"))
    data01 = _text(soup.select_one(".RaceData01"))
    distance_m, surface, going, post_time = _parse_data01(data01)
    race_class, class_label = _parse_class(soup, name)
    return {
        "race_id": race_id,
        "date": f"{race_id[0:4]}-??-??",  # 正確な月日はrace_listから補完してもよい
        "track": track,
        "race_no": race_no,
        "race_name": name,
        "distance_m": distance_m,
        "surface": surface,
        "going": going,
        "direction": DIRECTION.get(track),
        "race_class": race_class,      # 数値の格(1〜8)。不明はNone
        "class_label": class_label,
        "post_time": post_time,        # 発走時刻 "HH:MM"（RaceData01由来・追加取得なし）
    }


def _parse_data01(text: str) -> tuple[int | None, str | None, str | None]:
    """ '芝1600m' / '馬場:良' などから 距離・馬場種別・状態 を抽出。"""
    if not text:
        return None, None, None
    surface = None
    if "芝" in text:
        surface = "芝"
    elif "ダ" in text:
        surface = "ダート"
    elif "障" in text:
        surface = "障害"
    m = re.search(r"(\d{3,4})m", text)
    distance = int(m.group(1)) if m else None
    # 馬場は「馬場:稍重」「馬場:稍」等どちらの表記もあるので両対応（長い順に判定）
    g = re.search(r"馬場\s*[:：]\s*(稍重|不良|良|稍|重|不)", text)
    _GOING = {"稍": "稍重", "不": "不良"}
    going = None
    if g:
        going = _GOING.get(g.group(1), g.group(1))
    # 発走時刻「15:40発走」→ "15:40"。無ければ None。
    t = re.search(r"(\d{1,2}:\d{2})\s*発走", text)
    post_time = t.group(1) if t else None
    return distance, surface, going, post_time


def parse_shutuba(html: str, race_id: str, odds_map: dict[int, float] | None = None) -> dict:
    """出馬表HTMLを構造化。odds_map があれば単勝オッズ・人気を付与。"""
    soup = BeautifulSoup(html, "lxml")
    race = _race_header(soup, race_id)
    horses: list[dict] = []

    rows = soup.select("tr.HorseList")
    if not rows:
        log.warning("出馬表の行(tr.HorseList)が見つからない: %s", race_id)

    for tr in rows:
        # 馬番クラスは枠番が後ろに付く(Umaban1, Umaban2…)ので前方一致で取る
        umaban = _to_int(_text(tr.select_one("td[class^='Umaban']")))
        a = tr.select_one("td.HorseInfo a[href*='/horse/']") or tr.select_one(".HorseName a")
        name = _text(a)
        horse_id = None
        if a and a.has_attr("href"):
            hm = re.search(r"/horse/(\d+)", a["href"])
            horse_id = hm.group(1) if hm else None
        sex_age = _text(tr.select_one("td.Barei"))
        # 斤量は専用クラスが無く、性齢(Barei)の次のtd
        barei_td = tr.select_one("td.Barei")
        weight_carried = _to_float(_text(barei_td.find_next_sibling("td"))) if barei_td else None
        jockey = _text(tr.select_one("td.Jockey a")) or _text(tr.select_one("td.Jockey"))
        trainer = _text(tr.select_one("td.Trainer"))
        horse_weight = _text(tr.select_one("td.Weight"))  # 例 "468(+10)"

        h = {
            "umaban": umaban,
            "name": name,
            "horse_id": horse_id,
            "sex_age": sex_age,
            "jockey": jockey,
            "trainer": trainer,
            "weight_carried": weight_carried,
            "horse_weight": horse_weight,
            "odds_win": None,
            "popularity": None,
        }
        if odds_map and umaban in odds_map:
            h["odds_win"] = odds_map[umaban]
        if umaban is not None and name:
            horses.append(h)

    # オッズが付いていれば人気をオッズ昇順で算出
    rated = [h for h in horses if h["odds_win"]]
    for rank, h in enumerate(sorted(rated, key=lambda x: x["odds_win"]), start=1):
        h["popularity"] = rank

    race["horses"] = horses
    log.info("出馬表 %s: %d頭 (オッズ付与 %d頭)", race_id, len(horses), len(rated))
    return race


def parse_fukusho(html: str) -> dict[int, int]:
    """結果ページから複勝払戻 {馬番: 100円あたりの払戻円} を抽出。
    race.netkeiba (tr.Fukusho) と db.netkeiba (先頭セルが「複勝」の行) の両形式に対応。
    馬番列=全て1..18の数列、払戻列=「円」を含む or 全て100以上、で列を内容判別する
    （人気列も1..18なので、馬番は最初に該当した列を採用）。
    取れなければ {} を返す（呼び出し側はそのレースを回収率集計から除外する）。"""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr.Fukusho")
    if not rows:
        rows = []
        for tr in soup.find_all("tr"):
            c = tr.find(["th", "td"])
            if c is not None and c.get_text(strip=True) == "複勝":
                rows.append(tr)
    for tr in rows:
        cols = []
        for td in tr.find_all("td"):
            txt = td.get_text("\n")
            nums = [int(m.replace(",", "")) for m in re.findall(r"[\d,]+", txt)]
            nums = [n for n in nums if n > 0]
            cols.append((nums, "円" in txt))
        uma = next((n for n, _ in cols if n and all(1 <= x <= 18 for x in n)), None)
        pay = next((n for n, en in cols
                    if n and n is not uma and (en or all(x >= 100 for x in n))), None)
        if uma and pay and len(uma) == len(pay):
            return dict(zip(uma, pay))
    return {}


def get_win_odds(race_id: str) -> dict[int, float]:
    """単勝オッズをAPIから取得。{馬番: オッズ}。失敗時は空。"""
    url = f"{BASE_RACE}/api/api_get_jra_odds.html?race_id={race_id}&type=1&action=init"
    out: dict[int, float] = {}
    try:
        data = json.loads(get(url))
        odds = (data.get("data") or {}).get("odds") or {}
        win = odds.get("1") or {}  # "1" = 単勝
        for uma, arr in win.items():
            try:
                out[int(uma)] = float(arr[0])
            except (ValueError, TypeError, IndexError):
                continue
    except Exception as e:  # noqa
        log.warning("単勝オッズAPI失敗 %s : %s", race_id, e)
    return out


def get_place_odds(race_id: str, debug: bool = False) -> dict[int, tuple[float, float]]:
    """複勝オッズをAPIから取得。{馬番: (下限, 上限)}。失敗時は空。
    単勝(type=1)と同じ api_get_jra_odds を type=2 で叩く。netkeiba の複勝は
    レンジ(min,max)で返るため、配列の先頭2要素を (低, 高) として拾う。
    ※ レスポンス構造は単勝と同型と推定。実際の並びは Actions ログで要確認のため、
      debug=True で生JSONの該当部分を出力できるようにしてある（推測で確定させない）。"""
    url = f"{BASE_RACE}/api/api_get_jra_odds.html?race_id={race_id}&type=2&action=init"
    out: dict[int, tuple[float, float]] = {}
    try:
        raw = get(url)
        data = json.loads(raw)
        odds = (data.get("data") or {}).get("odds") or {}
        place = odds.get("2") or {}  # "2" = 複勝
        if debug:
            # 生構造を1頭ぶんだけダンプ（Actionsで実際の並びを確認する用）
            sample = next(iter(place.items()), None)
            log.info("複勝odds構造サンプル %s: %s", race_id, sample)
        for uma, arr in place.items():
            try:
                if isinstance(arr, (list, tuple)) and len(arr) >= 2:
                    lo, hi = float(arr[0]), float(arr[1])
                else:  # 単一値しか無い場合は下限=上限
                    lo = hi = float(arr[0] if isinstance(arr, (list, tuple)) else arr)
                if lo > hi:            # 念のため昇順に
                    lo, hi = hi, lo
                out[int(uma)] = (lo, hi)
            except (ValueError, TypeError, IndexError):
                continue
    except Exception as e:  # noqa
        log.warning("複勝オッズAPI失敗 %s : %s", race_id, e)
    return out


# ----------------------------------------------------------------------
# 結果
# ----------------------------------------------------------------------
def parse_result(html: str, race_id: str) -> dict:
    """結果HTMLから着順・馬番・馬名・確定オッズ・人気に加え、
    履歴DB用に 馬ID・騎手・上がり3F・通過順・斤量・馬体重 も抽出する。
    結果ページ1枚に全部載っているため、これが履歴の宝の山。
    """
    soup = BeautifulSoup(html, "lxml")
    race = _race_header(soup, race_id)
    horses = []
    for tr in soup.select("tr.HorseList"):
        finish = _to_int(_text(tr.select_one("td.Result_Num")))      # 着順(取消等はNone)
        umaban = _to_int(_text(tr.select_one("td.Num.Txt_C")))       # 馬番(枠Numと区別)
        a = tr.select_one("td.Horse_Info a[href*='/horse/']") or tr.select_one("td.Horse_Info a")
        name = _text(a)
        horse_id = None
        if a and a.has_attr("href"):
            hm = re.search(r"/horse/(\d+)", a["href"])
            horse_id = hm.group(1) if hm else None
        odds = _to_float(_text(tr.select_one("td.Odds.Txt_R")))      # 確定単勝オッズ
        pop = _to_int(_text(tr.select_one("td.Odds.Txt_C")))         # 人気
        jockey = _text(tr.select_one("td.Jockey"))                   # 騎手
        weight_carried = _to_float(_text(tr.select_one("td.Jockey_Info")))  # 斤量
        passage = _text(tr.select_one("td.PassageRate"))             # 通過順 例 "3-4"
        body_weight = _text(tr.select_one("td.Weight"))              # 馬体重 例 "414(+4)"
        # td.Time は3つで [走破タイム, 着差, 上がり3F] の順（診断で確定）
        times = [_text(t) for t in tr.select("td.Time")]
        run_time = times[0] if len(times) > 0 else None
        margin_inc = _margin_to_len(times[1]) if len(times) > 1 else None  # すぐ前との差(馬身)
        agari = None
        if len(times) > 2 and times[2] and re.fullmatch(r"\d{2}\.\d", times[2]):
            agari = float(times[2])
        if umaban is None:
            continue
        horses.append({
            "umaban": umaban, "name": name, "horse_id": horse_id,
            "finish_pos": finish, "odds_win": odds, "popularity": pop,
            "jockey": jockey, "weight_carried": weight_carried,
            "passage": passage, "agari": agari, "body_weight": body_weight,
            "run_time": run_time, "_margin_inc": margin_inc,
        })

    # 着差を「勝ち馬からの馬身」に積み上げ（着順順に加算）。勝ち馬=0.0
    cum = 0.0
    for h in sorted([x for x in horses if x["finish_pos"]], key=lambda x: x["finish_pos"]):
        cum += h.pop("_margin_inc", None) or 0.0
        h["margin"] = round(cum, 3)
    for h in horses:
        h.pop("_margin_inc", None)   # 着順不明馬の作業用キーを掃除
        h.setdefault("margin", None)
    race["horses"] = horses
    log.info("結果 %s: %d頭 (1着=%s)", race_id, len(horses),
             next((h["umaban"] for h in horses if h["finish_pos"] == 1), "?"))
    return race


# ----------------------------------------------------------------------
# 過去走（馬ごと）
# ----------------------------------------------------------------------
def parse_horse_pastruns(html: str, horse_id: str, n: int = 5) -> list[dict]:
    """db.netkeiba.com/horse/{id} の成績表から直近n走を抽出。"""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.db_h_race_results") or soup.select_one("table.nk_tb_common")
    runs: list[dict] = []
    if not table:
        log.warning("過去走テーブルが見つからない: horse %s", horse_id)
        return runs
    for tr in table.select("tr")[1:]:  # ヘッダ除く
        tds = [_text(td) for td in tr.select("td")]
        if len(tds) < 12:
            continue
        runs.append({
            "date": tds[0],          # 日付
            "track": tds[1],         # 開催
            "race_name": tds[4] if len(tds) > 4 else None,
            "rank": _to_int(tds[11]) if len(tds) > 11 else None,  # 着順(列位置は要検証)
        })
        if len(runs) >= n:
            break
    return runs


# ----------------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------------
def _text(el) -> str | None:
    if el is None:
        return None
    return re.sub(r"\s+", " ", el.get_text(strip=True)) or None


def _to_int(s):
    try:
        return int(re.sub(r"[^\d-]", "", s)) if s else None
    except (ValueError, TypeError):
        return None


def _to_float(s):
    try:
        return float(re.sub(r"[^\d.]", "", s)) if s else None
    except (ValueError, TypeError):
        return None
