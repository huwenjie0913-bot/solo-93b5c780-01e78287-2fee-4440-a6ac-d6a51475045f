"""pytest 公共夹具：把工作区与用户级 site-packages 加入路径。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
pylibs = os.path.join(ROOT, ".pylibs", "lib", "python3.11", "site-packages")
if os.path.isdir(pylibs) and pylibs not in sys.path:
    sys.path.append(pylibs)
