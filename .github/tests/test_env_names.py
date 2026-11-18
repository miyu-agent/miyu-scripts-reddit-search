# reddit_search.py 读新环境变量名的测试（审计 2026-11-18 #168）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网：fetch 换成桩，断网也能跑。HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "reddit_search.py"

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>r/python</title></feed>
"""


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("reddit_search_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    """每条测试一个临时目录，环境里去掉所有 MIYU_*，网络函数先换成直接失败的桩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = root
        self.mod = load()
        self.calls = []

        def no_network(url, timeout=25):
            self.calls.append(url)
            raise AssertionError(f"测试里不该联网：{url}")

        self.mod.fetch = no_network


class EnvNames(Sandbox):
    # 审计 #168：核心 FH-6 以后只给新名字 MIYU_CACHE_DIR，不再给 MIYU_SCRIPT_CACHE_DIR。
    # Reddit RSS 一分钟大约只给一次，答案要缓存十分钟。只设新名字时现在一概不缓存，第二次又去联网。
    def test_rss_cached_in_new_env_name(self):
        cache = self.root / "cache"
        os.environ["MIYU_CACHE_DIR"] = str(cache)
        self.assertNotIn("MIYU_SCRIPT_CACHE_DIR", os.environ)

        def fake_fetch(url, timeout=25):
            self.calls.append(url)
            return 200, {}, RSS

        self.mod.fetch = fake_fetch
        deadline = time.time() + 60
        self.assertEqual(self.mod.reddit_rss("/r/python/hot/.rss", {"limit": 10}, deadline), RSS)
        self.assertTrue(cache.is_dir() and any(p.suffix == ".xml" for p in cache.iterdir()),
                        "MIYU_CACHE_DIR 里没有 RSS 缓存")
        # 第二次应命中缓存，不再联网。
        self.calls.clear()
        self.assertEqual(self.mod.reddit_rss("/r/python/hot/.rss", {"limit": 10}, deadline), RSS)
        self.assertEqual(self.calls, [], "缓存在 MIYU_CACHE_DIR 里，第二次不该再联网")


if __name__ == "__main__":
    unittest.main()
