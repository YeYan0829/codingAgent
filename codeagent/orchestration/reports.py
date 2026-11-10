"""子调查报告的只读兼容；不推断正文是否解决了问题。"""


def normalize_child_report(payload):
    report = dict(payload)
    legacy = report.pop("unresolved", [])
    limitations = list(report.get("limitations", []))
    errors = []
    for item in legacy:
        if item == "done_when 尚无独立 grader 验证":
            note = "子任务结论未经独立核验"
        elif report.get("status") == "completed":
            note = item
        else:
            errors.append(str(item))
            continue
        if note not in limitations:
            limitations.append(note)
    if limitations or "limitations" in report:
        report["limitations"] = limitations
    if errors:
        report["error"] = "; ".join(dict.fromkeys(
            ([str(report["error"])] if report.get("error") else []) + errors))
    return report
