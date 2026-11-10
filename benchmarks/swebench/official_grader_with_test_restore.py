"""官方评分的测试重命名兼容入口；保持候选补丁、测试内容和判分规则不变。

仅修正eval.sh中已有的测试恢复命令：同时恢复rename来源及目标，移除
基线不存在的目标文件。旧CA-only入口保持原样，已冻结运行不受影响。
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import runpy
import shlex
import sys


REVISION = 'official-test-rename-restore-v1'


def _path(value):
    if value.startswith('"'):
        value = ast.literal_eval(value)
    if (not isinstance(value, str) or not value or PurePosixPath(value).is_absolute()
            or any(part in ('', '.', '..', '.git') for part in value.split('/'))
            or any(char in value for char in ('\x00', '\n', '\r'))):
        raise ValueError('测试补丁包含不支持的重命名路径')
    return value


def renamed_paths(test_patch):
    """只读取diff头中的rename元数据，不从测试代码猜测恢复范围。"""
    pairs = []
    source = None
    for line in test_patch.splitlines():
        if line.startswith('diff --git '):
            if source is not None:
                raise ValueError('测试重命名缺少目标路径')
        elif line.startswith('rename from '):
            if source is not None:
                raise ValueError('测试重命名来源重复')
            source = _path(line[len('rename from '):])
        elif line.startswith('rename to '):
            if source is None:
                raise ValueError('测试重命名缺少来源路径')
            pairs.append((source, _path(line[len('rename to '):])))
            source = None
    if source is not None:
        raise ValueError('测试重命名缺少目标路径')
    return pairs


def _restore_command(base_commit, paths):
    quote = shlex.quote
    commands = ['git cat-file -e ' + quote(base_commit + '^{commit}') + ' || exit $?']
    for path in dict.fromkeys(paths):
        path = _path(path)
        commands.append('if git cat-file -e ' + quote(base_commit + ':' + path) + ' 2>/dev/null; then '
            'git checkout ' + quote(base_commit) + ' -- ' + quote(path) + ' || exit $?; else '
            'rm -f -- ' + quote(path) + ' || exit $?; fi')
    return '( ' + '; '.join(commands) + ' ) || exit $?'


def repair_eval_script(eval_script, test_patch, base_commit):
    pairs = renamed_paths(test_patch)
    if not pairs:
        return eval_script, {'revision': REVISION, 'changed': False, 'renames': [], 'reset_commands': 0}
    if not re.fullmatch('[0-9a-f]{40}', base_commit):
        raise ValueError('重命名恢复必须绑定完整base commit')
    lines = eval_script.splitlines(keepends=True)
    changed = 0
    covered = set()
    for index, line in enumerate(lines):
        if not line.startswith('git checkout '):
            continue
        words = shlex.split(line)
        if len(words) < 4 or words[:3] != ['git', 'checkout', base_commit]:
            continue
        paths = words[3:]
        if paths[:1] == ['--']:
            paths = paths[1:]
        related = [pair for pair in pairs if any(path in paths for path in pair)]
        if not related:
            continue
        for source, destination in related:
            paths.extend((source, destination))
            covered.add((source, destination))
        lines[index] = _restore_command(base_commit, paths) + ('\n' if line.endswith('\n') else '')
        changed += 1
    if covered != set(pairs):
        raise ValueError('测试重命名缺少可识别的原始恢复入口，拒绝猜测')
    repaired = ''.join(lines)
    return repaired, {'revision': REVISION, 'changed': True, 'renames': pairs,
        'reset_commands': changed, 'original_eval_sha256': hashlib.sha256(eval_script.encode()).hexdigest(),
        'repaired_eval_sha256': hashlib.sha256(repaired.encode()).hexdigest(),
        'test_patch_sha256': hashlib.sha256(test_patch.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--test-restore-audit', type=Path)
    args, remaining = parser.parse_known_args()
    import swebench.harness.utils as utils
    original = utils.make_test_spec
    def make_test_spec(instance):
        script, audit = repair_eval_script(instance['eval_script'], instance.get('test_patch', ''), instance['base_commit'])
        if args.test_restore_audit:
            args.test_restore_audit.parent.mkdir(parents=True, exist_ok=True)
            with args.test_restore_audit.open('a') as stream:
                stream.write(json.dumps({'instance_id': instance['instance_id'], **audit}, ensure_ascii=False) + '\n')
        return original({**instance, 'eval_script': script})
    utils.make_test_spec = make_test_spec
    previous_argv = sys.argv
    try:
        wrapper = Path(__file__).with_name('official_grader_with_ca.py')
        sys.argv = [str(wrapper), *remaining]
        runpy.run_path(str(wrapper), run_name='__main__')
    finally:
        utils.make_test_spec = original
        sys.argv = previous_argv


if __name__ == '__main__':
    main()
