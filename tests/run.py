#!/usr/bin/env python
"""测试入口：先重定向存储到临时文件（见 tests/base.py），再发现并运行全部用例。
用法：
  .venv/bin/python tests/run.py            # 跑全部
  .venv/bin/python tests/run.py -v         # 详细
  .venv/bin/python tests/run.py tests.test_executor  # 跑单个模块
"""
import sys
import unittest
from pathlib import Path

# 把项目根加入 sys.path，保证 `import tests.base` 与 `import backend` 可用
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import 任何 backend 模块前设置 env；base 在首行完成。
import tests.base  # noqa: F401

if __name__ == "__main__":
    import os
    import shutil
    import subprocess

    argv = sys.argv[1:]
    loader = unittest.TestLoader()
    if argv and not argv[0].startswith("-"):
        suite = loader.loadTestsFromName(argv[0])
        argv = argv[1:]
    else:
        suite = loader.discover("tests", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in argv else 1)
    result = runner.run(suite)
    py_ok = result.wasSuccessful()

    # ---- 前端渲染回归测试（jsdom，可选）----
    # 仅在环境具备 node 且能找到 jsdom 时运行；缺失则跳过，不影响 Python 套件。
    fe_ok = True
    node_bin = shutil.which("node")
    if node_bin:
        cand = [
            os.path.expanduser("~/.workbuddy/binaries/node/workspace/node_modules"),
            os.path.join(ROOT, "node_modules"),
        ]
        node_path = os.pathsep.join([p for p in cand if os.path.isdir(p)])
        fe_test = os.path.join(ROOT, "tests", "test_frontend_render.js")
        if os.path.isfile(fe_test):
            env = dict(os.environ)
            if node_path:
                env["NODE_PATH"] = node_path
            print("\n--- 前端渲染回归测试 (jsdom) ---")
            r = subprocess.run([node_bin, fe_test], env=env, cwd=ROOT)
            fe_ok = r.returncode == 0
    else:
        print("\n[skip] 未找到 node，跳过前端渲染回归测试")

    sys.exit(0 if (py_ok and fe_ok) else 1)
