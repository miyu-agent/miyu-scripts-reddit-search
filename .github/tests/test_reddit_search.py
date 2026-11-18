# reddit_search.py 的测试（审计 2026-11-18）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网：走 main() 的那条用子进程跑脚本，子进程里先把 socket 的连接和解析都换成直接失败，
# 断网也能跑、有网也不会真连。HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import calendar
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "reddit_search.py"

# 子进程里先堵上网络再按 __main__ 跑脚本（照审计复现用的 nonet.py）
NO_NETWORK_RUNNER = """
import runpy, socket, sys
def blocked(*a, **k):
    raise OSError("network blocked by test")
socket.create_connection = blocked
socket.socket.connect = lambda self, *a: blocked()
socket.getaddrinfo = blocked
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
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

        def no_network(url, timeout=25):
            raise AssertionError(f"测试里不该联网：{url}")

        self.mod.fetch = no_network

    def run_script(self, args):
        """子进程跑脚本（网络堵死），返回 CompletedProcess。"""
        return subprocess.run([sys.executable, "-I", "-B", "-c", NO_NETWORK_RUNNER, str(SCRIPT)],
                              input=json.dumps(args), capture_output=True, text=True, timeout=10,
                              env=dict(os.environ), cwd=self.root)


class SubredditPath(Sandbox):
    # 审计 #175-a：subreddit/author 直接拼进 URL 路径，带空格时 urllib 在连接前抛 http.client.InvalidURL，
    # fetch 只接 HTTPError、URLError/TimeoutError/OSError，于是整段 traceback 漏出来。
    def test_space_in_subreddit_gives_friendly_error(self):
        proc = self.run_script({"subreddit": "foo bar"})
        self.assertNotIn("Traceback", proc.stderr, proc.stderr[-300:])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertTrue(proc.stdout.startswith("错误："), proc.stdout)


class IsoTimestamp(Sandbox):
    # 审计 #175-b：iso() 用 time.mktime(...) - time.timezone 把 UTC 时间转成时间戳，
    # mktime 按本地时区算、夏令时期间多扣一小时，所以 America/New_York 的 7 月差 3600 秒。
    def iso_in(self, tz, text):
        if not os.path.exists(f"/usr/share/zoneinfo/{tz}"):
            self.skipTest(f"本机没有时区数据 {tz}")
        old = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return self.mod.iso(text)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_summer_time_new_york(self):
        text = "2026-07-01T12:00:00+00:00"
        self.assertEqual(self.iso_in("America/New_York", text),
                         calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))

    # 防改坏（现在就绿）：冬天不在夏令时，结果本来就对。
    def test_winter_new_york(self):
        text = "2026-01-01T12:00:00+00:00"
        self.assertEqual(self.iso_in("America/New_York", text),
                         calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))


if __name__ == "__main__":
    unittest.main()
