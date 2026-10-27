#!/usr/bin/env python3
# Display name: Reddit search
# 显示名称：Reddit 检索
# Description: Search Reddit posts site-wide or in a subreddit, list a subreddit or user, find subreddits, and read a post with its comments.
# 描述：检索 Reddit：全站或版块内搜帖、列版块和用户的帖子、找版块、读一整条帖子和评论。
# Timeout: 150
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "query": {"type": "string", "description": "Keywords (Reddit search syntax works, e.g. \"flair:help\"), or a reddit link / post id to read that post with its comments."},
#   "subreddit": {"type": "string", "description": "Limit to one subreddit, r/ optional. Without query: list its posts."},
#   "author": {"type": "string", "description": "Limit to one redditor, u/ optional. Without query: list their posts and comments."},
#   "find_subreddit": {"type": "string", "description": "Look up subreddits by name or topic instead of searching posts."},
#   "sort": {"type": "string", "enum": ["relevance", "hot", "new", "top", "comments"], "description": "Default relevance for keywords, hot for a subreddit, new for a user."},
#   "time": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "all"], "description": "Window for relevance, top and comments. Default all; month for top."},
#   "limit": {"type": "integer", "description": "Posts or comments to return, 1-50; default 10 posts, 20 comments."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."}
# }}

"""Two routes, picked per request:

- Reddit's own RSS feeds: the only anonymous door left on reddit.com (the .json
  endpoints answer 403). Real search, site-wide, ranked by Reddit; no scores.
  The budget is about one request per minute per IP, so a 429 waits for the
  reset once, and answers are cached for ten minutes.
- Arctic Shift, the free archive: comment trees with scores. Used first for a
  thread, and as the fallback for lists when the RSS budget is spent. It has no
  site-wide keyword search, and scores fill in about 36 hours after posting.
"""

import hashlib
import html
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REDDIT = "https://www.reddit.com"
ARCTIC = "https://arctic-shift.photon-reddit.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
ATOM = "{http://www.w3.org/2005/Atom}"
CACHE_SECONDS = 600
MAX_WAIT = 65          # 一次调用里至多等一回 Reddit 的限流重置
SETTLE_SECONDS = 36 * 3600

POST_URL_RE = re.compile(r"reddit\.com/(?:(?:r|u|user)/[^/]+/)?comments/([a-z0-9]{4,10})|redd\.it/([a-z0-9]{4,10})", re.I)
BARE_ID_RE = re.compile(r"^(?:t3_)?([a-z0-9]{5,10})$", re.I)


class Failure(Exception):
    pass


class Limited(Failure):
    """Reddit 的 RSS 额度用完了，带着还要等几秒。"""

    def __init__(self, wait):
        super().__init__(f"Reddit 限流，约 {int(wait)} 秒后恢复")
        self.wait = wait


# ---------------------------------------------------------------- 缓存与 HTTP

def progress(text):
    """进度行：Miyu 边跑边给人看，不进交给 AI 的结果。"""
    print(f"miyu-progress: {text}", file=sys.stderr, flush=True)


def cache_path(name):
    base = os.environ.get("MIYU_SCRIPT_CACHE_DIR")
    if not base:
        return None
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, name)


def cached(url):
    path = cache_path(hashlib.sha1(url.encode()).hexdigest()[:16] + ".xml")
    if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_SECONDS:
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def store(url, text):
    path = cache_path(hashlib.sha1(url.encode()).hexdigest()[:16] + ".xml")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def reset_file():
    return cache_path("reddit-reset")


def known_wait():
    """上一次 429 记下的重置时刻，省得明知没额度还去撞一次。"""
    path = reset_file()
    try:
        with open(path) as f:
            return max(0.0, float(f.read()) - time.time())
    except (TypeError, OSError, ValueError):
        return 0.0


def note_reset(wait):
    path = reset_file()
    if path:
        with open(path, "w") as f:
            f.write(str(time.time() + wait))


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise Failure(f"连不上 {urllib.parse.urlsplit(url).netloc}：{getattr(err, 'reason', err)}")


def reddit_rss(path, params, deadline):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{REDDIT}{path}?{query}" if query else f"{REDDIT}{path}"
    text = cached(url)
    if text is not None:
        return text
    for _attempt in range(2):
        wait = known_wait()
        if wait:
            if time.time() + wait > deadline:
                raise Limited(wait)
            progress(f"Reddit 限流，等 {int(wait) + 1} 秒额度")
            time.sleep(wait + 1)
        status, headers, body = fetch(url)
        if status == 200:
            store(url, body)
            return body
        if status == 429:
            try:
                wait = float(headers.get("x-ratelimit-reset") or 60)
            except ValueError:
                wait = 60.0
            note_reset(wait)
            continue
        if status == 404:
            raise Failure("Reddit 说没有这个版块、用户或帖子（404）")
        if status == 403:
            raise Failure("Reddit 拒绝访问（403）：私有版块、被封的版块或用户")
        raise Failure(f"Reddit 返回 HTTP {status}")
    raise Limited(known_wait() or 60)


def arctic(path, params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{ARCTIC}{path}?{query}"
    for backoff in (1, 3, None):    # 关键词索引冷启动常回 Timeout，再试一次多半就成
        status, _headers, body = fetch(url, timeout=30)
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        error = str(data.get("error") or "")
        if status == 200 and not error:
            return data.get("data") or []
        if "timeout" in error.lower() and backoff:
            time.sleep(backoff)
            continue
        raise Failure(f"Arctic Shift 归档出错（HTTP {status}{'：' + error if error else ''}）")
    return []


# ---------------------------------------------------------------- 文本

class _Text(html.parser.HTMLParser):
    """RSS 里的正文是 HTML：转成给 AI 看的纯文本，链接留地址。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.href, self.skip = [], None, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("p", "div", "pre", "blockquote", "table", "tr"):
            self.out.append("\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "a":
            self.href = attrs.get("href")
        elif tag == "img" and attrs.get("src"):
            self.out.append(f"[图片 {attrs['src']}]")
        elif tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            last = next((c for c in reversed(self.out) if c.strip()), "")
            if self.href not in last and not last.startswith("[图片") and not self.href.startswith(REDDIT + "/user/"):
                self.out.append(f" ({self.href})")
            self.href = None
        elif tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(fragment):
    parser = _Text()
    parser.feed(fragment or "")
    text = "".join(parser.out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"…（共 {len(text)} 字）"


def ago(ts):
    if not ts:
        return "?"
    delta = time.time() - ts
    if delta < 3600:
        return f"{max(1, int(delta // 60))} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def iso(text):
    try:
        return time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (TypeError, ValueError):
        return None


def bare(value, prefix):
    value = (value or "").strip().strip("/")
    for p in (prefix + "/", "/" + prefix + "/"):
        if value.lower().startswith(p):
            value = value[len(p):]
    return value.strip("/")


# ---------------------------------------------------------------- RSS 解析

def entries(feed):
    try:
        root = ET.fromstring(feed)
    except ET.ParseError:
        raise Failure("Reddit 回的不是 RSS（可能是验证页），稍后再试")
    out = []
    for e in root.findall(ATOM + "entry"):
        cat = e.find(ATOM + "category")
        link = e.find(ATOM + "link")
        content = e.findtext(ATOM + "content") or ""
        item = {
            "id": e.findtext(ATOM + "id") or "",
            "title": e.findtext(ATOM + "title") or "",
            "author": (e.findtext(f"{ATOM}author/{ATOM}name") or "").replace("/u/", ""),
            "subreddit": cat.get("term") if cat is not None else "",
            "link": link.get("href") if link is not None else "",
            "time": iso(e.findtext(ATOM + "published") or e.findtext(ATOM + "updated")),
        }
        # 链接帖的正文末尾是「submitted by … [link] [comments]」：[link] 是外链，其余扔掉
        outside = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
        if outside and "reddit.com" not in outside.group(1) and "redd.it" not in outside.group(1):
            item["url"] = html.unescape(outside.group(1))
        content = re.split(r"&#32; submitted by &#32;", content)[0]
        content = re.sub(r'<a href="[^"]+">\[(?:link|comments)\]</a>', "", content)
        item["body"] = html_text(content)
        out.append(item)
    return out


# ---------------------------------------------------------------- 动作

def post_id_of(query):
    match = POST_URL_RE.search(query or "")
    if match:
        return (match.group(1) or match.group(2)).lower()
    match = BARE_ID_RE.match((query or "").strip())
    # 裸 id 只认带数字的，免得把 "python" 这种关键词当成帖子
    if match and re.search(r"\d", match.group(1)):
        return match.group(1).lower()
    return None


def render_posts(header, posts, limit, notes=()):
    lines = [header]
    lines += [f"> {n}" for n in notes]
    if not posts:
        lines.append("\n没有结果。")
    full = limit <= 5
    for i, p in enumerate(posts[:limit], 1):
        sub = p.get("subreddit") or ""
        meta = [f"r/{sub}" if sub and not sub.startswith(("u/", "u_")) else "", f"u/{p['author']}" if p.get("author") else "", ago(p.get("time"))]
        if p.get("score") is not None:
            meta.append(f"{p['score']}↑ {p.get('comments', 0)} 评论" if p.get("settled", True) else "赞数未结算")
        lines.append(f"\n{i}. **{p['title']}**\n   {' · '.join(m for m in meta if m)}\n   {p['link']}")
        if p.get("url"):
            lines.append(f"   外链：{p['url']}")
        body = clip(p.get("body"), 1500 if full else 300)
        if body:
            lines.append("   " + body.replace("\n", "\n   "))
    return "\n".join(lines)


def search(args, deadline):
    query, sub, author = args["query"], args["subreddit"], args["author"]
    limit, sort, window = args["limit"], args["sort"], args["time"]
    if query:
        q = query + (f" author:{author}" if author else "")
        params = {"q": q, "sort": sort or "relevance", "t": window or ("month" if sort == "top" else "all"),
                  "limit": limit, "type": "link"}
        path = "/search.rss"
        if sub:
            path, params["restrict_sr"] = f"/r/{sub}/search.rss", "on"
        scope = f"r/{sub}" if sub else "全站"
    elif sub:
        sort = sort if sort in ("hot", "new", "top") else "hot"
        path, params, scope = f"/r/{sub}/{sort}/.rss", {"limit": limit, "t": window or "month" if sort == "top" else None}, f"r/{sub}"
    else:
        path = f"/user/{author}/.rss"
        params, scope = {"limit": limit, "sort": sort if sort in ("new", "top", "hot") else "new", "t": window}, f"u/{author}"
    label = f"## Reddit · {scope}" + (f" · “{query}”" if query else "") + f" · {params.get('sort') or sort}"
    try:
        items = [e for e in entries(reddit_rss(path, params, deadline)) if not e["id"].startswith("t5_")]
    except Limited as limited:
        if not (sub or author):
            raise Failure(f"{limited}。全站搜索只有 Reddit 自己能做，过一会儿再试") from None
        try:
            rows = archive_list(args)
        except Failure as archive_error:
            raise Failure(f"{limited}；改走归档也不成：{archive_error}") from None
        return {"kind": "posts", "label": label + "（Reddit 限流，改走归档）", "scope": scope, "query": query,
                "sort": params.get("sort") or sort, "source": "arctic_shift", "notes": ["来自 Arctic Shift 归档：按时间倒序"],
                "posts": rows[:limit]}
    notes = ["Reddit RSS 不带赞数和评论数；排序是 Reddit 自己排的"]
    for item in items:
        if item["id"].startswith("t1_"):          # 用户页里的评论
            item["title"] = "评论：" + re.sub(r"^/u/\S+ on ", "", item["title"])
    return {"kind": "posts", "label": label, "scope": scope, "query": query, "sort": params.get("sort") or sort,
            "source": "reddit_rss", "notes": notes, "posts": items[:limit]}


def archive_list(args):
    rows = arctic("/api/posts/search", {"subreddit": args["subreddit"] or None, "author": args["author"] or None,
                                        "query": args["query"] or None, "limit": args["limit"], "sort": "desc"})
    return [archive_post(r) for r in rows]


def archive_post(r):
    created = r.get("created_utc")
    return {"title": r.get("title") or "", "author": r.get("author"), "subreddit": r.get("subreddit"),
            "time": created, "score": r.get("score"), "comments": r.get("num_comments"),
            "settled": bool(created and time.time() - created > SETTLE_SECONDS),
            "link": f"{REDDIT}/r/{r.get('subreddit')}/comments/{r.get('id')}",
            "url": r.get("url") if r.get("url") and "reddit.com" not in r.get("url") else "",
            "body": r.get("selftext") or ""}


def walk(nodes, depth, out, limit):
    for node in nodes or []:
        if len(out) >= limit or not isinstance(node, dict):
            return
        data = node.get("data") or {}
        if node.get("kind") == "more":
            if data.get("children"):
                out.append({"depth": depth, "more": len(data["children"])})
            continue
        if node.get("kind") != "t1":
            continue
        out.append({"depth": depth, "author": data.get("author") or "[deleted]", "score": data.get("score"),
                    "time": data.get("created_utc"), "body": data.get("body") or ""})
        replies = data.get("replies")
        if isinstance(replies, dict):
            walk((replies.get("data") or {}).get("children"), depth + 1, out, limit)


def thread(post_id, args, deadline):
    limit = args["limit"] if args["limit_given"] else 20
    try:
        rows = arctic("/api/posts/ids", {"ids": post_id})
        if not rows:
            raise Failure("归档里还没有这个帖子")
        post = archive_post(rows[0])
        tree = arctic("/api/comments/tree", {"link_id": "t3_" + post_id, "limit": limit})
        flat = []
        walk(tree, 0, flat, limit)
        note = "来自 Arctic Shift 归档，带赞数" + ("" if post["settled"] else "；帖子不到 36 小时，赞数还没结算")
        comments, source = flat, "arctic_shift"
    except Failure as archive_error:
        feed = reddit_rss(f"/comments/{post_id}/.rss", {"sort": args["sort"] if args["sort"] in ("top", "new") else "top",
                                                        "limit": limit}, deadline)
        items = entries(feed)
        if not items:
            raise Failure("Reddit 没回这个帖子的内容")
        post = items[0]
        comments = [{"depth": 0, "author": c["author"], "time": c["time"], "body": c["body"]} for c in items[1:]]
        note = f"来自 Reddit RSS（归档不可用：{archive_error}）：评论是平铺的，看不出谁回谁，没有赞数"
        source = "reddit_rss"
    return {"kind": "thread", "id": post_id, "source": source, "notes": [note], "post": post, "comments": comments}


def comment_text(c):
    if "more" in c:
        return f"…还有 {c['more']} 条回复没展开"
    if "score" in c:
        return f"u/{c['author']} · {c['score']}↑ · {ago(c['time'])}\n" + clip(c["body"], 1200)
    return f"u/{c['author']} · {ago(c['time'])}\n{clip(c['body'], 1200)}"


def render_thread(result):
    head = render_posts(f"## Reddit 帖子 · {result['id']}", [result["post"]], 1, result["notes"])
    comments = result["comments"]
    lines = [head, f"\n### 评论（{len(comments)} 条）"]
    if not comments:
        lines.append("还没有评论。")
    for c in comments:
        pad = "  " * c["depth"]
        lines.append(f"\n{pad}- " + comment_text(c).replace("\n", "\n" + pad + "  "))
    return "\n".join(lines)


def find_subreddits(name, args, deadline):
    try:
        items = entries(reddit_rss("/subreddits/search.rss", {"q": name, "limit": args["limit"]}, deadline))
        rows = [{"name": re.sub(r"^.*/r/([^/]+)/?$", r"\1", e["link"]), "title": e["title"], "description": e["body"]}
                for e in items]
        source = "reddit_rss"
    except Limited:
        data = arctic("/api/subreddits/search", {"subreddit_prefix": name, "limit": args["limit"],
                                                 "sort_type": "subscribers", "sort": "desc"})
        rows = [{"name": r.get("display_name"), "subscribers": r.get("subscribers"),
                 "description": r.get("public_description") or ""} for r in data]
        source = "arctic_shift"
    return {"kind": "subreddits", "query": name, "source": source, "subreddits": rows}


def render_subreddits(result):
    note = "（Reddit 限流，改走归档：只按名字前缀找）" if result["source"] == "arctic_shift" else ""
    lines = [f"## 找版块 · “{result['query']}”{note}"]
    if not result["subreddits"]:
        lines.append("\n没有结果。")
    for r in result["subreddits"]:
        title = r["title"] if "title" in r else f"{r.get('subscribers') or '?'} 订阅"
        desc = r["description"]
        lines.append(f"\n- **r/{r['name']}** {title}\n  {clip(desc, 200)}" if desc else f"\n- **r/{r['name']}** {title}")
    return "\n".join(lines)


def known_params():
    """开头 Parameters 里写的参数名：不认识的参数直接报错，免得写错的名字被悄悄当成没给。"""
    lines, inside = [], False
    with open(__file__, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                break
            if line.startswith("# Parameters:"):
                inside = True
            elif inside:
                lines.append(line[1:])
    return set(json.loads("".join(lines)).get("properties", {}))


def reject_unknown(args):
    unknown = sorted(set(args) - known_params())
    if unknown:
        raise Failure(f"不认识的参数：{'、'.join(unknown)}。能用的：{'、'.join(sorted(known_params()))}")


def arguments():
    try:
        args = json.load(sys.stdin)
    except ValueError as exc:
        raise Failure(f"参数不是 JSON：{exc}")
    if not isinstance(args, dict):
        raise Failure("参数要是一个 JSON 对象")
    if args.get("format") == "json":
        FORMAT[0] = "json"
    reject_unknown(args)
    if args.get("format") not in (None, "md", "json"):
        raise Failure(f"format 只认 md、json，不认 {args['format']}")
    text = lambda key: str(args.get(key) or "").strip()
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        raise Failure("limit 要是整数")
    return {"query": text("query"), "subreddit": bare(text("subreddit"), "r"),
            "author": bare(bare(text("author"), "u"), "user"), "find": bare(text("find_subreddit"), "r"),
            "sort": text("sort").lower(), "time": text("time").lower(), "limit": max(1, min(limit, 50)),
            "limit_given": args.get("limit") is not None}


FORMAT = ["md"]     # 读参数时一认出 json 就改，出错也照它交


def iso_utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def post_json(p):
    out = {"title": p["title"], "author": p.get("author"), "subreddit": p.get("subreddit"), "link": p["link"],
           "url": p.get("url") or None, "created": iso_utc(p.get("time")), "body": p.get("body") or ""}
    if p.get("score") is not None:
        out.update(score=p["score"], comments=p.get("comments"), score_settled=p.get("settled", True))
    return out


def comment_json(c):
    if "more" in c:
        return {"depth": c["depth"], "more_replies": c["more"]}
    out = {"depth": c["depth"], "author": c["author"], "created": iso_utc(c["time"]), "body": c["body"]}
    if "score" in c:
        out["score"] = c["score"]
    return out


def to_json(result):
    out = {"ok": True, "kind": result["kind"], "source": result["source"]}
    if result["kind"] == "posts":
        out.update(scope=result["scope"], query=result["query"] or None, sort=result["sort"],
                   notes=result["notes"], posts=[post_json(p) for p in result["posts"]])
    elif result["kind"] == "thread":
        out.update(id=result["id"], notes=result["notes"], post=post_json(result["post"]),
                   comments=[comment_json(c) for c in result["comments"]])
    else:
        out.update(query=result["query"], subreddits=result["subreddits"])
    return out


def render_md(result, limit):
    if result["kind"] == "posts":
        return render_posts(result["label"], result["posts"], limit, result["notes"])
    if result["kind"] == "thread":
        return render_thread(result)
    return render_subreddits(result)


def fail(message, fix=None, **extra):
    if FORMAT[0] == "json":
        print(json.dumps(dict({"ok": False, "error": message}, **({"fix": fix} if fix else {}), **extra),
                         ensure_ascii=False))
    else:
        print(f"错误：{message}" + (f"。{fix}" if fix else ""))
    sys.exit(1)


def main():
    deadline = time.time() + MAX_WAIT
    try:
        args = arguments()
        if args["find"]:
            result = find_subreddits(args["find"], args, deadline)
        elif post_id_of(args["query"]):
            result = thread(post_id_of(args["query"]), args, deadline)
        elif args["query"] or args["subreddit"] or args["author"]:
            result = search(args, deadline)
        else:
            raise Failure("给 query（关键词或帖子链接）、subreddit、author、find_subreddit 里的一个")
    except Limited as limited:
        fail(str(limited), "这一路一分钟大约只给一次，稍后再调", retry_after=int(limited.wait) + 1)
    except Failure as failure:
        fail(str(failure))
    print(json.dumps(to_json(result), ensure_ascii=False) if FORMAT[0] == "json" else render_md(result, args["limit"]))


if __name__ == "__main__":
    main()
