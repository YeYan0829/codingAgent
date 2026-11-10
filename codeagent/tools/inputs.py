"""本地工具的生效输入定义，生成schema并在执行前校验。

参考固定OpenHands Action的类型声明方式、文件编辑/view及glob/grep的职责划分；
本地保留相对路径、带行内容的搜索、独立shell和按用户轮次撤销，未宣称上游接口兼容。
来源和许可见 codeagent/prompts/licenses/README.md。
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.command_service import CommandLimits
from codeagent.tools.fs_read import MAX_SEARCH_CONTEXT_LINES

RelativePath = Annotated[str, Field(min_length=1, description="当前项目内相对路径；不能越界或访问禁止路径。")]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="read_file返回的完整文件SHA-256。提供时检查冲突；null或省略表示不额外检查。")]
EditPath = Annotated[str, Field(min_length=1, max_length=1024, description="当前项目内相对文件路径。")]
Pattern = Annotated[str, Field(min_length=1, max_length=256)]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyInput(ToolInput):
    pass


class DirectoryInput(ToolInput):
    path: RelativePath = "."


class TreeInput(DirectoryInput):
    max_depth: int = Field(default=2, ge=1, le=4, description="显示层数；不跟随符号链接。")


class ReadFileInput(ToolInput):
    path: RelativePath
    start_line: int = Field(default=1, ge=1, le=10**9, description="请求起始行（包含），不是文件总行数。字节续读不传此字段。")
    end_line: int | None = Field(default=None, ge=1, le=10**9, description="请求结束行（包含），不得小于start_line；大范围返回有界页。省略时最多1000行。")
    start_byte: int | None = Field(default=None, ge=0, description="长行的原始字节游标，使用上次next_start_byte；必须同时提供expected_sha256，不混用行范围。")
    expected_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def check_mode(self):
        if self.start_byte is not None:
            if {"start_line", "end_line"} & self.model_fields_set:
                raise ValueError("字节续读不能混用行范围")
            if self.expected_sha256 is None:
                raise ValueError("字节续读必须提供expected_sha256；使用上次返回的sha256")
        elif self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line不得小于start_line")
        return self


class FindFilesInput(DirectoryInput):
    include: list[Pattern] = Field(default_factory=list, max_length=20, description="按ripgrep --glob显式包含，例如[\"**/*.py\"]；可覆盖ignore/隐藏筛选，仍不能访问禁止路径。")
    exclude: list[Pattern] = Field(default_factory=list, max_length=20, description="排除这些glob；是本次筛选，不是权限限制。")
    include_hidden: bool = Field(default=False, description="扩大到隐藏文件，仍按ripgrep的ignore/include规则筛选，不绕过禁止路径。")
    max_results: int = Field(default=200, ge=1, le=1000)


class SearchTextInput(FindFilesInput):
    query: str = Field(min_length=1, max_length=4096, description="搜索内容；literal为完整文本，正则须显式选择regex。")
    mode: Literal["literal", "regex"] = Field(default="literal", description="默认literal；使用正则语法时必须显式选择regex。")
    case: Literal["sensitive", "insensitive", "smart"] = "smart"
    context_lines: int = Field(default=0, ge=0, description="匹配前后的上下文行数；超过10按10执行，结果说明实际值。")
    max_results: int = Field(default=100, ge=1, le=200, description="最多返回的匹配/上下文记录总行数。")


class GitDiffInput(DirectoryInput):
    context_lines: int = Field(default=3, ge=0, le=10)


class EditText(ToolInput):
    old_text: str = Field(description="逐字匹配且唯一定位的原文；空字符串仅用于填充空文件。")
    new_text: str = Field(description="替换内容，空字符串删除匹配片段。")


class EditFileInput(ToolInput):
    path: EditPath
    edits: list[EditText] = Field(min_length=1, max_length=AtomicEditService.MAX_OPERATIONS, description="同文件按顺序执行的替换。")
    expected_sha256: SHA256 | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_edit(cls, value):
        # 保留已有真实模型调用的无歧义修复；对模型只公开首选path/edits形式。
        if not isinstance(value, dict):
            return value
        value = dict(value)
        edits = value.get("edits")
        if not value.get("path") and isinstance(edits, list):
            paths = {item.get("path") for item in edits if isinstance(item, dict)}
            if len(paths) == 1 and isinstance(next(iter(paths)), str):
                value["path"] = paths.pop()
                value["edits"] = [{k: v for k, v in item.items() if k != "path"} if isinstance(item, dict) else item for item in edits]
        if "edits" not in value and {"old_text", "new_text"} <= value.keys():
            value["edits"] = [{"old_text": value.pop("old_text"), "new_text": value.pop("new_text")}]
        return value


class ReplaceTextInput(EditText):
    path: EditPath
    op: Literal["replace_text"] = "replace_text"
    expected_sha256: SHA256 | None = None


class CreateFileInput(ToolInput):
    path: EditPath
    content: str
    op: Literal["create_file"] = "create_file"
    overwrite: bool = Field(default=False, description="目标存在时才需要明确true；默认不覆盖。")


class DeleteFileInput(ToolInput):
    op: Literal["delete_file"]
    path: EditPath
    expected_sha256: SHA256 | None = None


class MoveFileInput(ToolInput):
    op: Literal["move_file"]
    source: EditPath
    destination: EditPath
    expected_sha256: SHA256 | None = None


class WorkspaceEditInput(ToolInput):
    operations: list[ReplaceTextInput | CreateFileInput | DeleteFileInput | MoveFileInput] = Field(min_length=1, max_length=AtomicEditService.MAX_OPERATIONS)


class FilesystemPermissionInput(ToolInput):
    capability: Literal["filesystem_read", "filesystem_write"]
    resource: str = Field(min_length=1, max_length=4096, description="所需宿主资源；不存在的写路径可能授权到现存父目录，以审批显示范围为准。")
    reason: str = Field(min_length=1, max_length=500)
    scope: Literal["once", "session"]


class NetworkPermissionInput(ToolInput):
    capability: Literal["network"]
    reason: str = Field(min_length=1, max_length=500)
    scope: Literal["once", "session"]


class CommandInput(ToolInput):
    command: str = Field(min_length=1, max_length=CommandLimits.MAX_COMMAND_CHARS, description="原样执行的Bash命令；允许项目内写入，shell副作用不在文件undo范围内。")
    cwd: RelativePath = Field(default=".", description="相对于当前Runtime上下文active_workspace的目录，默认项目根。")
    timeout_seconds: int = Field(default=CommandLimits.DEFAULT_TIMEOUT_SECONDS, ge=1, le=CommandLimits.MAX_TIMEOUT_SECONDS, description="单次命令硬超时，超时终止进程树；不是整轮工作期限。")
    purpose: Literal["utility", "validation"] = Field(default="utility", description="用途标签，不要求清单、不认证代码版本。")
    permissions: list[FilesystemPermissionInput | NetworkPermissionInput] = Field(default_factory=list, max_length=CommandLimits.MAX_PERMISSIONS, description="额外宿主资源或网络权限；按once/session审批，不可扩大或绕过禁止范围。")
