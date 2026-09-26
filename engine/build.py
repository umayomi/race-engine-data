#!/usr/bin/env python3
"""Race Engine 用 JSON ビルダー（Step 1: 騎手のみ）。

  python engine/build.py --date 20260926            # その日の全レース
  python engine/build.py --date 20260926 --race 202609040611   # 1レースだけ

出力:
  data/race-engine/{YYYYMMDD}/{racecourse_code}_{R}R.json   … 1レース=1JSON
  data/race-engine/{YYYYMMDD}/index.json                    … その日の一覧
  data/race-engine/index.json                               … 全日付の一覧（最新が先頭）
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scraper"), str(ROOT / "engine")]
import netkeiba as nk      # noqa: E402  (カチウマから流用・実証済み)
import umarengod as U      # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
SCHEMA_VERSION = "1.0"
OUT_DIR = ROOT / "data" / "race-engine"
RACECOURSE_CODE = {"札幌": "sapporo", "函館": "hakodate", "福島": "fukushima", "新潟": "niigata",
                   "東京": "tokyo", "中山": "nakayama", "中京": "chukyo", "京都": "kyoto",
                   "阪神": "hanshin", "小倉": "kokura"}


def _iso(d: str) -> str:
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


class Srch6Source:
    """umarengod の出馬表ページから、レース単位で全頭の父・母父『同コース成績』を取る。
    経路: 入口1回 → 日付×競馬場のレース一覧(場数) → 各レース(レース数)。
    2場開催なら 1+2+24=27 リクエストで1日ぶんが揃う（血統を馬ごとに引く方式の約1/20）。"""

    def __init__(self, session, race_date: str):
        self.session, self.race_date = session, race_date
        self.kd = f"{race_date[0:4]}-{race_date[4:6]}-{race_date[6:8]}"
        self.by_race: dict = {}          # (venue, race_no) -> {"meta":…, "horses":[…]}
        self.status: dict = {"status": "not_loaded"}
        self.requests = 0
        self.fetched_at = None

    def load(self) -> dict:
        r, st = U.get_with_retry(self.session, U.URL_SRCH6)
        self.requests += 1
        if r is None:
            self.status = {**st, "stage": "entry"}
            return self.status
        self.fetched_at = datetime.datetime.now(JST).isoformat(timespec="seconds")
        entry = U.decode(r)
        tabs = [t for t in U.parse_srch6_tabs(entry) if t["kd"] == self.kd]
        if not tabs:
            self.status = {"status": "not_found", "reason": f"date_tab_missing:{self.kd}",
                           "stage": "entry"}
            return self.status
        venues, seen = [], set()
        for t in tabs:                      # 同日の競馬場（重複除去）
            if t["cs"] not in seen:
                seen.add(t["cs"])
                venues.append(t)
        n_races, errs = 0, []
        for t in venues:
            rl, st2 = U.get_with_retry(self.session, U.srch6_list_url(t["ki"], t["kd"], t["cs"], t["bty"]))
            self.requests += 1
            if rl is None:
                errs.append({**st2, "venue": t["cs"], "stage": "list"})
                continue
            for sel in U.parse_srch6_list(U.decode(rl)):
                rr, st3 = U.get_with_retry(self.session, U.srch6_race_url(sel))
                self.requests += 1
                if rr is None:
                    errs.append({**st3, "venue": t["cs"], "race_no": sel["race_no"], "stage": "race"})
                    continue
                meta, horses = U.parse_srch6_race(U.decode(rr))
                if not meta["table_found"]:
                    errs.append({"status": "error", "reason": "table_not_found",
                                 "venue": t["cs"], "race_no": sel["race_no"], "stage": "race"})
                    continue
                key = (meta["venue"] or t["cs"], sel["race_no"])
                self.by_race[key] = {"meta": meta, "horses": horses}
                n_races += 1
        self.status = {"status": "ok" if n_races else "error", "races": n_races,
                       "venues": [t["cs"] for t in venues], "errors": errs[:10],
                       "fetched_at": self.fetched_at,
                       "leakage_safe": U.srch6_leakage_safe(self.fetched_at or "", self.race_date)}
        return self.status

    def get(self, racecourse: str, race_no: int, horse_name: str, surface, distance_m):
        """馬名で突き合わせて (sire_block, damsire_block) を返す。"""
        page = self.by_race.get((racecourse, race_no))
        if page is None:
            st = {"status": "error", "reason": f"srch6_page_missing:{racecourse}{race_no}R",
                  "source": "umarengod_srch6"}
            return st, dict(st)
        safe = self.status.get("leakage_safe", False)
        key = U.norm_horse_key(horse_name or "")
        hit = next((h for h in page["horses"] if U.norm_horse_key(h.get("name") or "") == key), None)
        if hit is None:
            st = {"status": "not_found", "reason": "horse_not_on_srch6_page",
                  "source": "umarengod_srch6", "name": None}
            return st, dict(st)
        return (U.srch6_pedigree_block(hit["sire"], racecourse, surface, distance_m, safe),
                U.srch6_pedigree_block(hit["damsire"], racecourse, surface, distance_m, safe))


class PedigreeCache:
    """種牡馬/母父ごとに1回だけPOSTする。産駒成績ページは全条件を1ページで返すため、
    「馬名」だけがキーになり、条件別の追加リクエストは不要。"""
    def __init__(self, session, race_date: str):
        self.session, self.race_date = session, race_date
        self.memo: dict = {}
        self.name_lists: dict = {}
        self.list_status: dict = {}
        self.requests = 0

    def _names(self, fld: str):
        if fld not in self.name_lists:
            self.requests += 1
            lst, st = U.fetch_name_list(self.session, fld)
            self.name_lists[fld] = lst
            self.list_status[fld] = st          # JSONに残す（失敗を見えなくしない）
            print(f"  umarengod {fld} 登録名リスト: {st}")
        return self.name_lists[fld]

    def get(self, fld: str, name: str):
        _, end = U.period_3y(self.race_date)
        key = (fld, name, end.isoformat())     # 期間を必ずキーに含める
        if key not in self.memo:
            self.requests += 1
            self.memo[key] = U.fetch_pedigree(self.session, fld, name, self.race_date,
                                              name_list=self._names(fld))
        return self.memo[key]


class PedigreeNames:
    """netkeiba の血統ページから 父・母父 を取得（馬ごとに1回）。"""
    def __init__(self):
        self.memo: dict = {}
        self.requests = 0

    def get(self, horse_id: str | None) -> dict:
        if not horse_id:
            return {"sire": None, "damsire": None, "error": "horse_id_missing"}
        if horse_id not in self.memo:
            self.requests += 1
            try:
                html = nk.get(f"{nk.BASE_DB}/horse/ped/{horse_id}/")
                self.memo[horse_id] = nk.parse_pedigree(html, horse_id)
            except Exception as e:  # noqa: BLE001
                self.memo[horse_id] = {"sire": None, "damsire": None,
                                       "error": f"network:{type(e).__name__}"}
        return self.memo[horse_id]


class JockeyTables:
    """(場, 芝ダ, 距離, 集計終了日) 単位のメモ。終了日を必ずキーに含める＝別日の流用が構造的に起きない。"""
    def __init__(self, session, race_date: str):
        self.session, self.race_date = session, race_date
        self.memo: dict = {}
        self.requests = 0

    def get(self, place: str, surface: str, distance_m: int):
        _, end = U.period_3y(self.race_date)
        key = (place, U.surface_param(surface), distance_m, end.isoformat())
        if key not in self.memo:
            if U.surface_param(surface) is not None:
                self.requests += 1
            self.memo[key] = U.fetch_jockey_table(self.session, surface, distance_m,
                                                  self.race_date, place)
        return self.memo[key]


def build_race(race: dict, race_date: str, jt: JockeyTables,
               pn: "PedigreeNames | None" = None, pc: "PedigreeCache | None" = None,
               s6: "Srch6Source | None" = None) -> dict:
    start, end = U.period_3y(race_date)
    d1_ok = (datetime.datetime.strptime(race_date, "%Y%m%d").date() - end).days == 1
    course, surface, dist = race.get("track"), race.get("surface"), race.get("distance_m")

    if surface and dist and course:
        place_tbl, place_st = jt.get(course, surface, dist)
        all_tbl, all_st = jt.get("ALL", surface, dist)
    else:
        err = {"status": "error", "reason": "race_condition_missing"}
        place_tbl, place_st, all_tbl, all_st = {}, err, {}, err

    horses = []
    for h in race.get("horses", []):
        jk = U.resolve_jockey(h.get("jockey") or "", course, surface, dist,
                              place_tbl, place_st, all_tbl, all_st)
        sire = damsire = {"status": "not_implemented"}
        if s6 is not None:
            sire, damsire = s6.get(course, race.get("race_no"), h.get("name"), surface, dist)
        elif pn is not None and pc is not None:
            names = pn.get(h.get("horse_id"))
            out2 = {}
            for fld, key in (("father", "sire"), ("mfather", "damsire")):
                nm = names.get(key)
                if not nm:
                    out2[key] = {"name": None, "status": "error" if names.get("error") else "not_found",
                                 "reason": names.get("error") or "pedigree_name_unavailable"}
                    continue
                rows, st = pc.get(fld, nm)
                out2[key] = U.resolve_pedigree(nm, rows, st, course, surface, dist)
            sire, damsire = out2["sire"], out2["damsire"]
        horses.append({
            "horse_number": h.get("umaban"),
            "horse_name": h.get("name"),
            "horse_id_netkeiba": h.get("horse_id"),
            "sex_age": h.get("sex_age"),
            "weight_carried": h.get("weight_carried"),
            "jockey": jk,
            "sire": sire,
            "damsire": damsire,
        })

    def _states(key):
        return [x[key]["status"] for x in horses]

    def _complete(key):
        # unsupported（障害戦など、そもそも umarengod に該当データが無い条件）は
        # 取得失敗ではないので欠損扱いにしない。
        return bool(horses) and all(s in ("ok", "insufficient_sample", "not_found", "unsupported")
                                    for s in _states(key))
    jk_states = _states("jockey")
    jockey_complete = _complete("jockey")
    has_ped = (pc is not None) or (s6 is not None)
    sire_complete = _complete("sire") if has_ped else False
    damsire_complete = _complete("damsire") if has_ped else False
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "umarengod",
        "generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "race": {
            "date": _iso(race_date),
            "race_id_netkeiba": race.get("race_id"),
            "racecourse": course,
            "racecourse_code": RACECOURSE_CODE.get(course),
            "race_number": race.get("race_no"),
            "race_name": race.get("race_name"),
            "surface": surface,
            "distance": dist,
            "post_time": race.get("post_time"),
            "race_class": race.get("race_class"),
        },
        "aggregation_period": {"start": start.isoformat(), "end": end.isoformat(),
                               "leakage_safe": d1_ok},
        "fetch_status": {"jockey_primary": place_st, "jockey_all": all_st,
                         "pedigree_source": ("srch6" if s6 else ("etcfatherm" if pc else None)),
                         "pedigree_name_list": (pc.list_status if pc else None),
                         "srch6": (s6.status if s6 else None)},
        "horses": horses,
        "quality": {
            "jockey_data_complete": jockey_complete,
            "jockey_status_counts": {s: jk_states.count(s) for s in sorted(set(jk_states))},
            "sire_data_complete": sire_complete,
            "damsire_data_complete": damsire_complete,
            "sire_status_counts": {s: _states("sire").count(s) for s in sorted(set(_states("sire")))},
            "damsire_status_counts": {s: _states("damsire").count(s) for s in sorted(set(_states("damsire")))},
            "pending": [] if has_ped else ["sire", "damsire"],
            "d1_cutoff_verified": d1_ok,
            "full_r1_ready": bool(jockey_complete and sire_complete and damsire_complete and d1_ok),
        },
    }


def race_filename(race: dict) -> str:
    code = RACECOURSE_CODE.get(race.get("track")) or (race.get("race_id") or "x")[4:6]
    return f"{code}_{int(race.get('race_no') or 0)}R.json"


def update_top_index(date: str) -> None:
    p = OUT_DIR / "index.json"
    idx = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"dates": []}
    dates = set(idx.get("dates", [])) | {date}
    idx = {"updated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
           "dates": sorted(dates, reverse=True)}
    p.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--race", default=None, help="netkeiba race_id（1レースだけ作る場合）")
    ap.add_argument("--no-pedigree", action="store_true", help="血統を取得しない（騎手のみ）")
    ap.add_argument("--pedigree-source", choices=("srch6", "etcfatherm"), default="srch6",
                    help="血統の取得元。srch6=出馬表ページ(軽量・既定) / etcfatherm=種牡馬ページ(従来)")
    args = ap.parse_args()
    datetime.datetime.strptime(args.date, "%Y%m%d")

    import requests
    session = requests.Session()
    jt = JockeyTables(session, args.date)
    use_s6 = (not args.no_pedigree) and args.pedigree_source == "srch6"
    s6 = None
    if use_s6:
        s6 = Srch6Source(session, args.date)
        print("血統: srch6（出馬表ページ）から取得します")
        print("  srch6 読み込み:", json.dumps(s6.load(), ensure_ascii=False)[:300])
        if s6.status.get("status") != "ok":
            print("  → srch6 が使えないため etcfatherm 方式にフォールバックします")
            s6, use_s6 = None, False
    pn = None if (args.no_pedigree or use_s6) else PedigreeNames()
    pc = None if (args.no_pedigree or use_s6) else PedigreeCache(session, args.date)

    race_ids = [args.race] if args.race else nk.find_race_ids(args.date)
    if not race_ids:
        print(f"{args.date}: 開催なし or レース一覧取得失敗")
        return
    day_dir = OUT_DIR / args.date
    day_dir.mkdir(parents=True, exist_ok=True)

    entries, n_ok = [], 0
    for rid in race_ids:
        try:
            html = nk.get(f"{nk.BASE_RACE}/race/shutuba.html?race_id={rid}")
            race = nk.parse_shutuba(html, rid)
        except Exception as e:  # noqa: BLE001
            print(f"出馬表失敗 {rid}: {e}")
            continue
        out = build_race(race, args.date, jt, pn, pc, s6)
        fn = race_filename(race)
        (day_dir / fn).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        q = out["quality"]
        entries.append({"file": fn, "racecourse": race.get("track"), "race_number": race.get("race_no"),
                        "race_name": race.get("race_name"), "jockey_data_complete": q["jockey_data_complete"],
                        "full_r1_ready": q["full_r1_ready"]})
        n_ok += 1
        print(f"  {fn}: 騎手 {q['jockey_status_counts']}"
              + (f" / 父 {q['sire_status_counts']} / 母父 {q['damsire_status_counts']}"
                 if (pc or s6) else ""))

    idx_path = day_dir / "index.json"
    old = json.loads(idx_path.read_text(encoding="utf-8")).get("races", []) if idx_path.exists() else []
    merged = {e["file"]: e for e in old}
    merged.update({e["file"]: e for e in entries})
    idx_path.write_text(json.dumps({"date": _iso(args.date), "races": sorted(
        merged.values(), key=lambda e: (e["racecourse"] or "", e["race_number"] or 0))},
        ensure_ascii=False, indent=2), encoding="utf-8")
    update_top_index(args.date)
    print(f"完了: {n_ok}/{len(race_ids)}R / umarengod騎手 {jt.requests}回"
          + (f" / srch6 {s6.requests}回" if s6 else "")
          + (f" / umarengod血統 {pc.requests}回 / netkeiba血統 {pn.requests}回" if pc else ""))


if __name__ == "__main__":
    main()
