# HAL50 测评工具与配置

本目录提供固定 HAL SWE-bench Verified Mini 50 题的题单、两版正式测评配置、环境预检和评分适配。成绩、案例、成本与逐题检阅方法见[测评报告](../docs/HAL50_EVALUATION.md)。

## 题单与配置

| 文件 | 内容 |
| --- | --- |
| [固定题单](swebench/selections/hal-verified-mini-50.json) | 50题身份、固定来源版本与执行排序 |
| [v0.5.0配置](swebench/configs/v0.5.0-evaluation-candidate-hal-mini-50.json) | 35/50正式测评使用的模型、上下文和实验限制 |
| [v0.6.0配置](swebench/configs/v0.6.0-hal50.json) | 40/50正式测评的构造配置、实际SDK参数、网络与逐题实验限制 |
| [价格快照](swebench/prices/glm-5.3-standard-api-2026-09-09.json) | 两版GLM-5.3用量的费用计算口径 |

v0.6.0配置中的`effective_sdk`来自50题选定尝试的1,581次实际请求核对。原始补丁、独立评分和生效配置随对应Release的测评证据附件提供。

## 环境预检与独立评分

| 脚本 | 用途 |
| --- | --- |
| [check_repository_preflight.py](swebench/check_repository_preflight.py) | 在实际题目容器检查读取、搜索、编辑、命令、网络边界与撤销 |
| [prepare_pristine_cache.py](swebench/prepare_pristine_cache.py) | 准备并核对固定镜像的干净项目输入 |
| [finalize_preflight.py](swebench/finalize_preflight.py) | 将环境与官方测试预检绑定到题单和源码版本 |
| [official_grader_with_ca.py](swebench/official_grader_with_ca.py) | 独立评分入口与证书适配 |
| [official_grader_with_test_restore.py](swebench/official_grader_with_test_restore.py) | 按固定任务配置恢复评分测试，覆盖测试文件重命名 |

这些脚本的完整参数可用`--help`查看。测评环境另需固定版本的任务仓库、官方评分器和题目镜像。生成端通过普通Runtime调查与修改，评分器在独立容器读取候选补丁和官方测试。

串行批次入口位于Runtime的`codeagent swebench-batch`；环境、网络和评分实现见[架构说明](../docs/ARCHITECTURE.md#将执行核心接入编辑器和测评环境)。先核对题单、配置与资源，再启动正式运行；日常产品任务沿用普通Runtime的执行与权限机制。
