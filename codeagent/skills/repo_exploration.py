REPO_EXPLORATION_STRATEGY = """
仓库探索优先级：
1. 先查看顶层目录。
2. 优先阅读 README、pyproject/package 配置、docs 和 tests。
3. 对源码目录做小范围读取，不一次性读取过大的文件。
4. 遇到敏感文件只说明被策略阻止，不尝试绕过。
"""
