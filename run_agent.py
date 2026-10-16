"""启动入口：python run_agent.py doctor --ping

和 `python -m zhipu_agent.main` 等价。单独做一个入口，是因为有些机器上的 Python
被设成了隔离模式（sys.flags.isolated == 1），当前目录和 PYTHONPATH 都不会进搜索路径，
`-m` 会找不到包。这里由脚本自己把仓库根目录放进去。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zhipu_agent.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
