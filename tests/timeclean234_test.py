"""输出行首内部时间标签的严格清洗（无模型、无运行数据）。

python -I -B timeclean234_test.py <框架目录>
可追加 --compare <另一源码目录> ...，逐份验证并比较共通输出。
本脚本不创建文件；任何断言失败都以非零状态退出。
"""

import ast
import asyncio
import copy
from datetime import datetime
import importlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CHECKS = []
TAG = "〔消息时间：2026-11-03 01:02〕"


def check(label, actual, expected):
    passed = actual == expected
    CHECKS.append(passed)
    print(("OK " if passed else "FAIL ") + label, flush=True)
    if not passed:
        print(f"  实际: {actual!r}\n  期望: {expected!r}", flush=True)


def load(root, index):
    package = "multichat"
    directory = root / package
    name = f"_timeclean234_{index}"
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, package, importlib.import_module(name + ".sanitize")


def check_send_cycle(root, package, name, sanitize, prefix, now):
    """执行真实 say/build_turns 方法体；只替换传输、存盘和等待，不导入平台或模型。"""
    source = root / package / "engine.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    engine = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Engine")
    methods = [node for node in engine.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in {"say", "build_turns"}]
    check(prefix + "集成使用实际发送和上下文方法", {node.name for node in methods}, {"say", "build_turns"})
    pacing = importlib.import_module(name + ".pacing")
    message_time = importlib.import_module(name + ".jsutil").message_time

    async def no_wait(*args):
        return None

    namespace = {"asyncio": SimpleNamespace(sleep=no_wait), "pace": pacing.schedule,
                 "message_time": message_time}
    if (root / package / "quotes.py").is_file():
        namespace["render_quote"] = importlib.import_module(name + ".quotes").render_quote
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    user = {"id": "u1", "role": "user", "text": "我发的原文含有" + TAG, "ts": now - 60000}
    original_user = copy.deepcopy(user)
    history, sent, saved = [user], [], []

    async def send(text):
        sent.append(text)
        return True

    def save():
        saved[:] = copy.deepcopy(history)

    store = SimpleNamespace(
        new_msg=lambda role, text: {"id": f"h{len(history)}", "role": role, "text": text, "ts": now},
        save_messages=save,
    )
    chat = SimpleNamespace(
        _leaving=lambda: False, _farewell_edge=None, _farewell_grace=False, recall_cut=False,
        sender=SimpleNamespace(typing=no_wait, send=send), store=store, history=history,
        _recent_message=copy.deepcopy, _commit_farewell=lambda: None, _asked=None,
        # 这里发送普通回复，不处于主动开场；该状态提交为无操作，主动边界由 farewell235 覆盖。
        _commit_proactive_opening=lambda message: None,
        _publish_recent_read=lambda: None, llm=SimpleNamespace(supports=lambda _: False),
        memory=SimpleNamespace(working_set=lambda rows, **kwargs: rows), round_ids=set(),
    )
    raw, next_turns = TAG * 3 + "我听到了。", []
    counts = []
    for _ in range(3):
        count = asyncio.run(namespace["say"](chat, sanitize.to_messages(raw)))
        counts.append(count)
        next_turns = namespace["build_turns"](chat, history)
        # 下一轮模型模仿上一条 assistant 的请求格式，验证不会越存越多。
        raw = next_turns[-1]["content"]
    check(prefix + "三轮实际发送条数", counts, [1, 1, 1])
    check(prefix + "节拍交给假QQ的内容没有内部标签", sent, ["我听到了。"] * 3)
    check(prefix + "实际say写入存档的是干净正文", [row["text"] for row in saved[1:]], ["我听到了。"] * 3)
    check(prefix + "下一轮每条assistant只附加一层时间", [row["content"].count("〔消息时间：")
          for row in next_turns if row["role"] == "assistant"], [1, 1, 1])
    check(prefix + "发送存档不修改用户原文", history[0], original_user)


def run(root, index):
    name, package, sanitize = load(root, index)
    prefix = f"{root.name}/{package}: "
    strip = getattr(sanitize, "strip_leading_time_labels", None)
    check(prefix + "提供严格行首清洗入口", callable(strip), True)
    # 旧版负对照继续执行，仍可明确显示哪些实际输出没有被修复。
    strip = strip if callable(strip) else lambda value: value
    to_messages = sanitize.to_messages

    success = [
        ("单个标签", TAG + "晚上好。", "晚上好。"),
        ("横向空白", " \t\u3000" + TAG + " \t你好。", "你好。"),
        ("连续五个标签超过旧分条长度", TAG * 5 + "我在这里。", "我在这里。"),
        ("连续标签间的空白", TAG + " \t" + TAG + "晚安。", "晚安。"),
        ("只有标签", TAG * 4, ""),
        ("闰年", "〔消息时间：2024-02-29 23:59〕好。", "好。"),
        ("世纪闰年", "〔消息时间：2000-02-29 00:00〕好。", "好。"),
        ("合法年份下界", "〔消息时间：0001-01-01 00:00〕好。", "好。"),
        ("合法年份上界", "〔消息时间：9999-12-31 23:59〕好。", "好。"),
    ]
    for label, raw, expected in success:
        check(prefix + label, strip(raw), expected)

    preserved = [
        ("正文中的标签", "我看到" + TAG + "了。"),
        ("普通年月日时分", "2026-11-03 01:02 我还醒着。"),
        ("普通时分", "01:02 也太晚了。"),
        ("未命中时不改普通空白", " \t今天聊得很开心。  "),
        ("中文双引号", "“" + TAG + "”是什么意思？"),
        ("中文单引号", "‘" + TAG + "’是标签。"),
        ("英文双引号", '"' + TAG + '" 是标签。'),
        ("英文单引号", "'" + TAG + "' 是标签。"),
        ("直角引号", "「" + TAG + "」是标签。"),
        ("代码引用", "`" + TAG + "` 是标签。"),
        ("引用行", "> " + TAG + "这是一段引用。"),
        ("残缺结束括号", TAG[:-1] + " 你好。"),
        ("残缺前缀", "消息时间：2026-11-03 01:02〕你好。"),
        ("未知时间", "〔消息时间：时间不详〕你好。"),
        ("相似括号", "【消息时间：2026-11-03 01:02】你好。"),
        ("相似字段", "〔发送时间：2026-11-03 01:02〕你好。"),
        ("半角字段冒号", "〔消息时间:2026-11-03 01:02〕你好。"),
        ("含秒并非受支持格式", "〔消息时间：2026-11-03 01:02:03〕你好。"),
        ("跨行日期", "〔消息时间：2026-11-03\n01:02〕你好。"),
        ("跨行标签前缀", "〔消息\r\n时间：2026-11-03 01:02〕你好。"),
        ("单行接口不跨行清洗", TAG + "\n你好。"),
        ("解释是什么意思", TAG + " 是什么意思？"),
        ("解释表示", TAG + "表示记录时间。"),
        ("解释代表", TAG + "代表消息发出的时间。"),
        ("解释指的是", TAG + "指的是哪一天？"),
        ("解释这个标签", TAG + "这个标签是程序加的。"),
        ("解释一种标签", TAG + "是一种时间标签。"),
        ("解释程序附加", TAG + "是程序附加的记录信息。"),
    ]
    for label, raw in preserved:
        check(prefix + label, strip(raw), raw)

    invalid = [
        "0000-01-01 00:00", "1900-02-29 00:00", "2026-02-29 12:00",
        "2026-04-31 12:00", "2026-00-03 01:02", "2026-13-03 01:02",
        "2026-11-00 01:02", "2026-11-32 01:02", "2026-11-03 24:00",
        "2026-11-03 23:60", "2026-1-03 01:02", "2026-11-3 01:02",
        "2026-11-03 1:02", "2026-11-03 01:2", "２０２６-11-03 01:02",
    ]
    for stamp in invalid:
        raw = "〔消息时间：" + stamp + "〕你好。"
        check(prefix + "非法日期/格式 " + stamp, strip(raw), raw)
    bad = "〔消息时间：2026-02-30 01:02〕"
    check(prefix + "不能跳过非法首标签搜后面", strip(bad + TAG + "你好。"), bad + TAG + "你好。")
    check(prefix + "有效首标签后保留非法标签", strip(TAG + bad + "你好。"), bad + "你好。")

    cases = [
        ("分条之前清理连续标签", TAG * 100 + "你" * 125, ["你" * 60, "你" * 60, "你" * 5]),
        ("每个原始行分别清理", TAG + "早。\r\n" + TAG * 3 + "晚安。", ["早。", "晚安。"]),
        ("纯标签行不发空消息", TAG + "\n" + TAG + "你好。", ["你好。"]),
        ("分行残片不拼回去删", "〔消息时间：2026-11-03\n01:02〕你好。", ["〔消息时间：2026-11-03", "01:02〕你好。"]),
        ("正文标签不会成为第二次清理目标", TAG + "我看到" + TAG + "了。", ["我看到" + TAG + "了。"]),
        ("引用中的标签保留", "“" + TAG + "”是什么意思？", ["“" + TAG + "”是什么意思？"]),
        ("解释标签保留", TAG + "表示记录时间。", [TAG + "表示记录时间。"]),
        ("不删除清洗动作后才显露的标签", "（微笑）" + TAG + "你好。", [TAG + "你好。"]),
        ("带横线的列表", "- " + TAG + "你好。", ["你好。"]),
        ("带星号的列表", "* " + TAG + "你好。", ["你好。"]),
        ("带圆点的列表", "• " + TAG + "你好。", ["你好。"]),
        ("编号列表", "1. " + TAG + "你好。", ["你好。"]),
        ("顿号编号列表", "2、" + TAG + "你好。", ["你好。"]),
        ("列表中的引用保留", "1. “" + TAG + "”是什么意思？", ["“" + TAG + "”是什么意思？"]),
        ("普通标签之后不删中间标签", "时间：" + TAG + "你好。", ["时间：" + TAG + "你好。"]),
    ]
    outputs = []
    for label, raw, expected in cases:
        actual = to_messages(raw)
        outputs.append(actual)
        check(prefix + label, actual, expected)

    speaker = "小伴"
    check(prefix + "角色名前缀后清理", to_messages(speaker + "：" + TAG + "你好。"), ["你好。"])
    check(prefix + "角色名加列表前缀后清理", to_messages(speaker + "：- 1. " + TAG + "你好。"), ["你好。"])
    check(prefix + "其他人名不能越过", to_messages("别人：" + TAG + "你好。"), ["别人：" + TAG + "你好。"])
    output_strip = getattr(sanitize, "_strip_output_time_labels", lambda value: value)
    check(prefix + "强调不是列表", output_strip("*" + TAG + "*"), "*" + TAG + "*")
    if package == "multichat":
        profile = importlib.import_module(name + ".profile")
        with profile.use({"character_name": "测试角色"}):
            check(prefix + "框架保留动态角色名", to_messages("测试角色：" + TAG + "你好。"), ["你好。"])
            check(prefix + "不误认旧默认角色名", to_messages("小伴：" + TAG + "你好。"), ["小伴：" + TAG + "你好。"])
    else:
        check(prefix + "保留现有英文角色别名", to_messages("Firefly: " + TAG + "你好。"), ["你好。"])

    winext = importlib.import_module(name + ".winext")
    now = int(datetime(2026, 11, 3, 1, 2).timestamp() * 1000)
    for group in (False, True):
        block = winext.time_block(now, group=group)
        check(prefix + f"时间说明区分正文与风格 group={group}",
              all(word in block for word in ("程序附加", "不是任何人", "风格范例")), True)
        check(prefix + f"时间说明仍带完整日期 group={group}",
              "2026-11-03" in block and "01:02" in block, True)
    check_send_cycle(root, package, name, sanitize, prefix, now)
    return outputs


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    roots = [Path(sys.argv[1]).resolve()]
    if "--compare" in sys.argv:
        roots.extend(Path(value).resolve() for value in sys.argv[sys.argv.index("--compare") + 1:])
    baseline = None
    for index, root in enumerate(roots):
        try:
            outputs = run(root, index)
        except Exception as exc:
            check(f"{root.name}: 脚本异常", f"{type(exc).__name__}: {exc}", "无异常")
            continue
        if baseline is None:
            baseline = outputs
        else:
            check(f"{root.name}: 共通清洗输出与首份一致", outputs, baseline)
    passed = sum(CHECKS)
    print(f"timeclean234 {passed}/{len(CHECKS)}", flush=True)
    return 0 if CHECKS and all(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
