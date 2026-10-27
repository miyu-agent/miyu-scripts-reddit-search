# Reddit 检索

Miyu 脚本包，带一件工具 `reddit_search`：全站或版块内搜帖、列版块和用户的帖子、找版块、读一整条帖子和评论。照参数自己挑做哪一样，不用选动作。

| 参数 | 说明 |
|---|---|
| `query` | 关键词（认 Reddit 的搜索写法，如 `flair:help`）；放帖子链接或 id 就读那一帖和评论 |
| `subreddit` | 限一个版块，`r/` 可写可不写；不给 `query` 就列这个版块的帖子 |
| `author` | 限一个用户，`u/` 可写可不写；不给 `query` 就列这人的帖子和评论 |
| `find_subreddit` | 按名字、话题找版块 |
| `sort` | `relevance`、`hot`、`new`、`top`、`comments`；关键词默认 `relevance`，版块默认 `hot`，用户默认 `new` |
| `time` | `hour`、`day`、`week`、`month`、`year`、`all`；`top` 默认 `month` |
| `limit` | 1–50，帖子默认 10 条、评论默认 20 条；5 条以内带长一些的正文 |
| `format` | `md`（默认）给人和 AI 读；`json` 给代码用，比如 `miyu call` 接 `jq`、Python。出错也是 JSON：`{"ok": false, "error": …}` |

## 数据从哪来

- **Reddit 自己的 RSS**：reddit.com 匿名还开着的只有这一路（`.json` 接口都回 403）。能全站搜、按 Reddit 自己的排序，但不带赞数。额度大约一个 IP 一分钟一次：撞上限流等到重置再试一次，答过的十分钟内再问直接用缓存（放在 `MIYU_SCRIPT_CACHE_DIR`）。
- **Arctic Shift 归档**：读帖子时先走它，评论是树形、带赞数（发帖约 36 小时后才回填，之前标「未结算」）。列表撞上 Reddit 限流时也退到它，但它没有全站关键词搜索。归档不可用时，读帖子退回 Reddit RSS：评论是平铺的，看不出谁回谁。

只用标准库。
