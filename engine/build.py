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


class PedigreeCache:
    """種牡馬/母父ごとに1回だけPOSTする。産駒成績ページは全条件を1ページで返すため、
    「馬名」だけがキーになり、条件別の追加リクエストは不要。"""
    def __init__(self, session, race_date: str):
        self.session, self.race_date = session, race_date
        self.memo: dict = {}
        self.requests = 0

    def get(self, fld: str, name: str):
        _, end = U.period_3y(self.race_date)
        key = (fld, name, end.isoformat())     # 期間を必ずキーに含める
        if key not in self.memo:
            self.requests += 1
            self.memo[key] = U.fetch_pedigree(self.session, fld, name, self.race_date)
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
               pn: "PedigreeNames | None" = None, pc: "PedigreeCache | None" = None) -> dict:
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
        if pn is not None and pc is not None:
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
        return bool(horses) and all(s in ("ok", "insufficient_sample", "not_found")
                                    for s in _states(key))
    jk_states = _states("jockey")
    jockey_complete = _complete("jockey")
    sire_complete = _complete("sire") if pc is not None else False
    damsire_complete = _complete("damsire") if pc is not None else False
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
        "fetch_status": {"jockey_primary": place_st, "jockey_all": all_st},
        "horses": horses,
        "quality": {
            "jockey_data_complete": jockey_complete,
            "jockey_status_counts": {s: jk_states.count(s) for s in sorted(set(jk_states))},
            "sire_data_complete": sire_complete,
            "damsire_data_complete": damsire_complete,
            "sire_status_counts": {s: _states("sire").count(s) for s in sorted(set(_states("sire")))},
            "damsire_status_counts": {s: _states("damsire").count(s) for s in sorted(set(_states("damsire")))},
            "pending": [] if pc is not None else ["sire", "damsire"],
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
    args = ap.parse_args()
    datetime.datetime.strptime(args.date, "%Y%m%d")

    import requests
    session = requests.Session()
    jt = JockeyTables(session, args.date)
    pn = None if args.no_pedigree else PedigreeNames()
    pc = None if args.no_pedigree else PedigreeCache(session, args.date)

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
        out = build_race(race, args.date, jt, pn, pc)
        fn = race_filename(race)
        (day_dir / fn).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        q = out["quality"]
        entries.append({"file": fn, "racecourse": race.get("track"), "race_number": race.get("race_no"),
                        "race_name": race.get("race_name"), "jockey_data_complete": q["jockey_data_complete"],
                        "full_r1_ready": q["full_r1_ready"]})
        n_ok += 1
        print(f"  {fn}: 騎手 {q['jockey_status_counts']}"
              + (f" / 父 {q['sire_status_counts']} / 母父 {q['damsire_status_counts']}" if pc else ""))

    idx_path = day_dir / "index.json"
    old = json.loads(idx_path.read_text(encoding="utf-8")).get("races", []) if idx_path.exists() else []
    merged = {e["file"]: e for e in old}
    merged.update({e["file"]: e for e in entries})
    idx_path.write_text(json.dumps({"date": _iso(args.date), "races": sorted(
        merged.values(), key=lambda e: (e["racecourse"] or "", e["race_number"] or 0))},
        ensure_ascii=False, indent=2), encoding="utf-8")
    update_top_index(args.date)
    print(f"完了: {n_ok}/{len(race_ids)}R / umarengod騎手 {jt.requests}回"
          + (f" / umarengod血統 {pc.requests}回 / netkeiba血統 {pn.requests}回" if pc else ""))


if __name__ == "__main__":
    main()
