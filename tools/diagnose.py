#!/usr/bin/env python3
"""Step 0: umarengod の構造調査（棚卸し）＋ 血統の取得元調査。

実ページを取得して diagnostics/ に保存・コミットする。開発者（Claude等）は
リポジトリの tarball からこの生HTMLを読んで、推測でなく実物で実装を決める。

調べること:
  A. robots.txt（クロール可否。Disallow は取得しない）
  B. etcsrch4.php のフォーム（place / crs / course2 / rc / grade の実際の option value）
  C. etcsrch4.php への実POST（騎手表のヘッダーを実物で確認）
  D. etcfatherm.php のフォームと、父/母父の GET（UTF-8 と Shift_JIS/EUC-JP の両方で試す）
  E. srch4.php（騎手個別ページ）
  F. トップページからのリンク棚卸し（同一ドメイン・上限あり・2秒間隔）
  G. netkeiba の血統ページ候補（父・母父の名前をどこから取れるか）
"""
from __future__ import annotations
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "diagnostics"
RAW = OUT / "raw"
BASE = "https://umarengod.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
INTERVAL = 2.0
MAX_SAVE = 400_000          # 1ページあたり保存する最大文字数（リポジトリ肥大防止）

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
RP = urllib.robotparser.RobotFileParser()
REPORT: dict = {"generated_at": None, "checks": []}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:90]


def allowed(url: str) -> bool:
    try:
        return RP.can_fetch(UA, url)
    except Exception:  # noqa: BLE001
        return True


def fetch(url: str, method: str = "GET", data=None, raw_url: bool = False, check_robots: bool = True):
    """取得して (text, info)。raw_url=True なら URL を再エンコードしない（文字コード試験用）。
    robots.txt 自体の取得は check_robots=False（パーサ未初期化だと全拒否になり、ルールが一切
    適用されなくなるため）。"""
    info = {"url": url, "method": method}
    if check_robots and url.startswith(BASE) and not allowed(url):
        info["skipped"] = "robots_disallow"
        return None, info
    try:
        if method == "POST":
            r = S.post(url, data=data, timeout=40)
        elif raw_url:
            req = requests.Request("GET", url)
            p = S.prepare_request(req)
            p.url = url                     # requests による再エンコードを防ぐ
            r = S.send(p, timeout=40)
        else:
            r = S.get(url, timeout=40)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"[:200]
        return None, info
    finally:
        time.sleep(INTERVAL)
    info["status"] = r.status_code
    r.encoding = r.apparent_encoding
    info["encoding"] = r.encoding
    return r.text, info


def analyze(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None
    forms = []
    for f in soup.find_all("form"):
        fields = []
        for inp in f.find_all("input"):
            fields.append({"tag": "input", "name": inp.get("name"), "type": inp.get("type"),
                           "value": inp.get("value")})
        for sel in f.find_all("select"):
            opts = [{"value": o.get("value"), "text": o.get_text(strip=True)}
                    for o in sel.find_all("option")]
            fields.append({"tag": "select", "name": sel.get("name"),
                           "options_count": len(opts), "options": opts[:120]})
        forms.append({"action": f.get("action"), "method": (f.get("method") or "GET").upper(),
                      "fields": fields})
    tables = []
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        head = []
        for tr in rows[:3]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                head.append(cells[:25])
        tables.append({"rows": len(rows), "first_rows": head})
    links = sorted({a["href"] for a in soup.find_all("a", href=True)})
    txt = soup.get_text(" ", strip=True)
    return {"title": title, "forms": forms, "tables": tables,
            "links_count": len(links), "links": links[:300],
            "has_stats_table": ("出走" in txt and "複勝率" in txt),
            "mentions_sire": any(w in txt for w in ("父", "種牡馬")),
            "text_head": txt[:400]}


def save(name: str, html: str | None, info: dict) -> dict:
    rec = {"name": name, **info}
    if html is not None:
        (RAW / f"{_slug(name)}.html").write_text(html[:MAX_SAVE], encoding="utf-8")
        rec["saved"] = f"diagnostics/raw/{_slug(name)}.html"
        rec["analysis"] = analyze(html)
    REPORT["checks"].append(rec)
    a = rec.get("analysis", {})
    print(f"[{name}] status={info.get('status')} skip={info.get('skipped')} err={info.get('error')} "
          f"title={a.get('title')!r} forms={len(a.get('forms', []))} tables={len(a.get('tables', []))} "
          f"stats_table={a.get('has_stats_table')}")
    return rec


def enc_query(params: dict, encoding: str) -> str:
    return "&".join(f"{k}={urllib.parse.quote(str(v).encode(encoding))}" for k, v in params.items())


def main():
    OUT.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    REPORT["generated_at"] = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).isoformat(timespec="seconds")

    # A. robots.txt
    txt, info = fetch(f"{BASE}/robots.txt", check_robots=False)
    (OUT / "robots.txt").write_text(txt or f"(取得失敗) {info}", encoding="utf-8")
    if info.get("status") in (401, 403):
        RP.disallow_all = True                    # 標準の解釈: 認証拒否なら全面不可
        RP.parse([])
    elif info.get("status") == 200 and txt:
        RP.parse(txt.splitlines())
    else:
        RP.parse([])                              # 404 等は制限なし（標準の解釈）
    REPORT["robots"] = {"status": info.get("status"), "text": (txt or "")[:3000]}
    print("robots.txt:", info.get("status"))

    # B/C. 騎手集計フォームと実POST
    save("etcsrch4_form", *fetch(f"{BASE}/etcsrch4.php"))
    sys.path.insert(0, str(ROOT / "engine"))
    import umarengod as U  # noqa: E402
    probe_day = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y%m%d")
    html, info = fetch(U.URL_JOCKEY, "POST", U.jockey_payload("芝", 1600, probe_day, "中山"))
    rec = save("etcsrch4_post_nakayama_turf1600", html, info)
    if html:
        tbl, meta = U.parse_stats_table(html, "騎手名")
        rec["parser_check"] = {"meta": meta, "sample": list(tbl.items())[:3]}
        print("  既存パーサ検証:", meta)

    # D. 血統: フォーム + 父/母父 を文字コード別に試す
    save("etcfatherm_form", *fetch(f"{BASE}/etcfatherm.php"))
    for fld, name in (("father", "ドレフォン"), ("mfather", "フジキセキ")):
        for enc in ("utf-8", "shift_jis", "euc_jp"):
            url = f"{BASE}/etcfatherm.php?{enc_query({'fld': fld, 'pvalue': name}, enc)}"
            save(f"etcfatherm_{fld}_{enc}", *fetch(url, raw_url=True))

    # E. 騎手個別ページ
    for enc in ("utf-8", "shift_jis"):
        url = f"{BASE}/srch4.php?{enc_query({'ks': '川田将雅'}, enc)}"
        save(f"srch4_kawada_{enc}", *fetch(url, raw_url=True))

    # F. トップからのリンク棚卸し
    top_html, top_info = fetch(f"{BASE}/")
    top = save("top", top_html, top_info)
    max_pages = int(os.environ.get("MAX_PAGES") or 30)
    seen, crawled = set(), []
    for href in top.get("analysis", {}).get("links", []):
        url = urllib.parse.urljoin(f"{BASE}/", href)
        pu = urllib.parse.urlparse(url)
        if pu.netloc != "umarengod.com" or url in seen:
            continue
        seen.add(url)
        if len(crawled) >= max_pages:
            break
        crawled.append(url)
        save(f"page_{pu.path}_{pu.query}", *fetch(url))
    REPORT["crawl"] = {"max_pages": max_pages, "crawled": crawled,
                       "discovered_same_domain": len(seen)}

    # F1b. 血統POSTが table_not_found になる名前の生レスポンスを保存（原因確定用）
    #      リストには存在する名前なのに失敗する → 実際に何が返るのかを見る。
    import umarengod as _U
    probe_date = os.environ.get("PROBE_DATE") or (datetime.date.today() - datetime.timedelta(days=4)).strftime("%Y%m%d")
    for fld, nm in (("father", "ドレフォン"), ("father", "モーリス"),
                    ("mfather", "クロフネ"), ("mfather", "フジキセキ"),
                    ("father", "Giant's Causeway"), ("father", "Giant's Causeway\u00a0(米)")):
        html, info = fetch(_U.URL_PEDIGREE, "POST", _U.pedigree_payload(fld, nm, probe_date))
        rec = save(f"ped_post_{fld}_{_slug(nm)}", html, info)
        if html:
            rows, meta = _U.parse_pedigree_table(html)
            rec["pedigree_parse"] = {"meta": meta, "sample": rows[:2]}
            print(f"    → {fld} {nm!r}: rows={meta['rows']} table_found={meta['table_found']}")

    # F2. 出馬表系ページ（父・母父の同コース成績が1ページに揃う画面の調査）
    #     全レース対応か重賞限定か、URLに race_id/日付をどう渡すかを確認する。
    entry = save("srch6_entry", *fetch(f"{BASE}/srch6.php"))
    # srch6 の JS コメントに GET 相当のURLが明記されている:
    #   p=1 … 日付×競馬場のレース一覧 / p=2 … 個別レースの登録馬一覧(父・母父の同コース成績つき)
    kd = os.environ.get("ENTRY_DATE") or ""      # 例 2026-09-20
    if not kd:
        m = re.search(r'srch6_post_tab\(\s*\d+\s*,\s*\d+\s*,\s*"([\d-]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)',
                      entry.get("analysis", {}).get("text_head", "") or "")
        kd = ""
    js = ""
    try:
        js = (RAW / "srch6_entry.html").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    tabs = re.findall(r'srch6_post_tab\((\d+),\s*(\d+),\s*"([\d-]+)",\s*"([^"]+)",\s*(\d+)\)', js)
    if kd:
        tabs = [t for t in tabs if t[2] == kd] or tabs
    seen_tab = set()
    for p, ki, d, cs, bty in tabs[:4]:
        key = (d, cs)
        if key in seen_tab:
            continue
        seen_tab.add(key)
        u = f"{BASE}/srch6.php?p=1&ki={ki}&kd={d}&cs={urllib.parse.quote(cs)}&bty={bty}&seni=1"
        rec = save(f"srch6_list_{d}_{cs}", *fetch(u, raw_url=True))
        # レース一覧から個別レース(p=2)へ
        sels = re.findall(r'srch6_post_sel\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)',
                          (RAW / f"{_slug('srch6_list_' + d + '_' + cs)}.html").read_text(encoding="utf-8")
                          if rec.get("saved") else "")
        for p2, ki2, i0, i1, i2, r2, bty2 in sels[:3]:
            u2 = (f"{BASE}/srch6.php?p={p2}&ki={ki2}&i0={i0}&i1={i1}&i2={i2}&r={r2}"
                  f"&bty={bty2}&seni=1")
            rr = save(f"srch6_race_{d}_{cs}_{i0}_{i1}_{i2}", *fetch(u2, raw_url=True))
            aa = rr.get("analysis", {})
            if aa.get("mentions_sire"):
                print("    ★ 登録馬一覧(父・母父つき)候補:", u2)
    REPORT["entry_pages"] = {"tabs_found": len(tabs), "tabs_used": list(seen_tab)}
    for c in REPORT["checks"]:
        a = c.get("analysis", {})
        if a and ("登録馬一覧" in (a.get("text_head") or "") or "産駒の同コース" in (a.get("text_head") or "")):
            print("  ★ 父・母父の同コース成績ページ候補:", c["name"], c.get("url"))

    # G. netkeiba の血統ページ候補
    sys.path.insert(0, str(ROOT / "scraper"))
    import netkeiba as nk  # noqa: E402
    hid = os.environ.get("HORSE_ID") or "2021103932"
    rid = os.environ.get("RACE_ID") or "202601010811"
    for nm, url in ((f"netkeiba_ped_{hid}", f"{nk.BASE_DB}/horse/ped/{hid}/"),
                    (f"netkeiba_horse_{hid}", f"{nk.BASE_DB}/horse/{hid}/"),
                    (f"netkeiba_shutuba_past_{rid}", f"{nk.BASE_RACE}/race/shutuba_past.html?race_id={rid}")):
        try:
            html = nk.get(url)
            save(nm, html, {"url": url, "status": 200})
        except Exception as e:  # noqa: BLE001
            save(nm, None, {"url": url, "error": f"{type(e).__name__}: {e}"[:200]})

    (OUT / "summary.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# umarengod 診断 {REPORT['generated_at']}", "",
             f"robots.txt status: {REPORT['robots']['status']}", "",
             "| name | status | title | forms | tables | stats表 | 父/種牡馬の語 |",
             "|---|---|---|---|---|---|---|"]
    for c in REPORT["checks"]:
        a = c.get("analysis", {})
        lines.append(f"| {c['name']} | {c.get('status') or c.get('skipped') or c.get('error')} | "
                     f"{(a.get('title') or '')[:30]} | {len(a.get('forms', []))} | "
                     f"{len(a.get('tables', []))} | {a.get('has_stats_table')} | {a.get('mentions_sire')} |")
    (OUT / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"完了: {len(REPORT['checks'])}件 → diagnostics/")


if __name__ == "__main__":
    main()
