"""随 Runtime 发布的固定预加载技能；不发现或执行仓库中的指令文件。"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    version: int
    content: str

    @property
    def identity(self) -> str:
        return hashlib.sha256(f"{self.name}\n{self.version}\n{self.content}".encode()).hexdigest()


@lru_cache(maxsize=1)
def load_builtin_skill(name: str) -> BuiltinSkill:
    if name != "behavior-contract":
        raise ValueError("unknown builtin skill: " + name)
    content = files("codeagent.skills").joinpath("behavior_contract", "SKILL.md").read_text(encoding="utf-8")
    return BuiltinSkill(name, 2, content)


def render_behavior_contract_skill(role: str, *, delegation: bool = True) -> str:
    """delegation=False 时去掉分工段落，避免提示要求使用本轮不存在的子 Agent 工具。"""
    if role not in {"main", "readonly_child", "worker_child"}:
        raise ValueError("unsupported skill role: " + role)
    skill = load_builtin_skill("behavior-contract")
    content = skill.content
    if role != "main" or not delegation:
        content = "\n\n".join(p for p in content.split("\n\n") if not p.startswith("可分开的工作"))
    responsibility = (
        ("你选择本次相关的调查项，整合证据、实施并运行验证；可将范围明确的独立任务交给具备相应工具的子Agent。" if delegation else
         "你选择本次相关的调查项，整合证据、实施并运行验证。")
        if role == "main" else
        ("你只回答本次 goal 的相关部分，不扩张任务。只读文件、提出可核实结论和建议检查；不执行命令或测试。"
         if role == "readonly_child" else
         "你只完成本次goal，按角色工具和权限调查、执行或实现；报告实际结果与缺口，不递归委派。")
    )
    return (f"\n\n内置预加载 Skill：{skill.name}；version={skill.version}；sha256={skill.identity}。\n"
            "这是调查方法，不是本题事实、标准答案或额外授权；实际角色、任务与工具边界保持优先。\n"
            + content + "\n本次 Skill 职责：" + responsibility)
