# race-engine-data

umarengod の条件別成績を取得し、ChatGPT などから **GET だけで読める JSON** として公開する。
騎手取得部分は umayomi/kachiuma で本番稼働中のコードを流用・改良したもの。

## 進捗

| Step | 内容 | 状態 |
|---|---|---|
| 0 | umarengod の構造棚卸し・血統の取得元調査（diagnose） | 実装済み・実行待ち |
| 1 | 騎手の条件別成績 → 1レース1JSON | 実装済み |
| 2 | 父・母父の条件別成績 | Step 0 の結果を見て実装 |
| 3 | その他 umarengod 情報 | Step 0 の棚卸し結果から選定 |

## JSON の場所（ChatGPT に渡す URL）

```
全日付の一覧   https://raw.githubusercontent.com/umayomi/race-engine-data/main/data/race-engine/index.json
その日の一覧   https://raw.githubusercontent.com/umayomi/race-engine-data/main/data/race-engine/20260926/index.json
1レース        https://raw.githubusercontent.com/umayomi/race-engine-data/main/data/race-engine/20260926/hanshin_11R.json
```

競馬場コード: sapporo hakodate fukushima niigata tokyo nakayama chukyo kyoto hanshin kokura

## ChatGPT 向け: JSON の読み方

- `aggregation_period.end` はレース前日（D-1）。当日の成績は含まれない。`leakage_safe: true` を確認すること。
- 騎手ブロックの `status` で必ず分岐すること。**数値だけを見て判断しないこと。**

| status | 意味 | 扱い |
|---|---|---|
| `ok` | 取得成功・出走5回以上 | そのまま使用可 |
| `insufficient_sample` | 取得成功だが出走5回未満 | `adjusted_place_rate`（縮小済み）を使い、信頼度は低いとみなす |
| `not_found` | 条件内の出走が無い、または騎手名を照合できなかった | 「成績不明」。0% とみなさない |
| `error` | 取得失敗（通信・ページ構造） | 「データ欠損」。0% とみなさない |
| `unsupported` | 障害戦など未対応条件 | 「対象外」 |
| `not_implemented` | 未実装項目（Step 2 前の血統） | 無視 |

- `raw_place_rate` は生の複勝率、`adjusted_place_rate` は少数サンプルを 25% 方向へ縮小した値
  （`(複勝率×出走 + 0.25×8) / (出走 + 8)`）。予想には `adjusted_place_rate` を推奨。
- `fallback_used: true` のとき、その騎手は当該競馬場で出走5回未満だったため全場×同距離の成績。
  元の競馬場での出走数は `primary_condition.starts`。
- `quality.full_r1_ready` が `true` のときだけ「騎手・父・母父すべて揃った完全データ」。
  現在は血統未実装のため常に `false`。

## 取得の流儀（変えないこと）

- 集計期間は (D-3年)〜(D-1)。キャッシュキーには必ず集計終了日を含める。
- 取得失敗を 0 や空で代用しない（status で必ず区別する）。
- umarengod へのリクエスト間隔は 1 秒以上。同一条件は1回の実行内でメモ化。
- robots.txt の Disallow は取得しない。
- ワークフローの `run:` ブロックに `${{ }}` を書かない（入力は `env:` 経由）。
