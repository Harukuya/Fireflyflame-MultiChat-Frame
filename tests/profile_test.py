"""Offline persona/schedule contract checks. No runtime data or model calls."""
import ast
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from multichat import profile, persona, winext, schedule

passed = 0


def check(ok, name):
    global passed
    assert ok, name
    passed += 1
    print("OK", name)


def stamp(hour, minute=0, day=2):
    return int(datetime(2026, 11, day, hour, minute).timestamp() * 1000)


profile.configure({}, ROOT / "prompts")
check(profile.bot_name() == "小伴", "default name")
check(profile.primary_name() == "主要联系人", "default primary label")
check("小伴" in persona.build_system_prompt(), "identity assembled")
check("本轮要回应" in persona.FORMAT and all("如果对方的话虽然多，但内容比较空、重复或只是随口闲聊，你也少说一点，简单接住就好。" in text
      for text in (persona.FORMAT, persona.build_return_context(100, "休息"), winext.group_block("测试群", ""), winext.group_catchup_context("休息"))),
      "relative reply length follows substance; empty or repetitive talk gets a brief response")
check("想做的事" in persona.build_system_prompt(channel="tools"), "tool protocol")
check("第一次交流" in persona.build_first_contact_context(), "first contact no invented relation")
check(not schedule.state_at(stamp(7, 59)).online, "before online")
check(schedule.state_at(stamp(8)).online, "online boundary included")
check(schedule.state_at(stamp(22, 59)).online, "last online minute")
check(not schedule.state_at(stamp(23)).online, "offline boundary excluded")
check(schedule.online_since(stamp(10, 23)) == stamp(8), "online start accurate minute")
check(schedule.offline_after(stamp(10, 23)) == stamp(23), "next offline accurate minute")
check(schedule.online_since(stamp(2)) is None, "offline has no online start")
check(schedule.offline_after(stamp(2)) is None, "offline has no next-online-segment end")
profile.configure({"online_start": "20:00", "online_end": "04:00"}, ROOT / "prompts")
check(schedule.state_at(stamp(1)).online and not schedule.state_at(stamp(12)).online, "cross midnight online")
check(schedule.online_since(stamp(1)) == stamp(20, day=1), "cross midnight previous start")
check(schedule.offline_after(stamp(1)) == stamp(4), "cross midnight next end")
profile.configure({"schedule_mode": "always"}, ROOT / "prompts")
check(schedule.state_at(stamp(3)).online, "always online")
check(schedule.online_since(stamp(3)) is None and schedule.offline_after(stamp(3)) is None, "always has no finite boundaries")
profile.configure({"schedule_mode": "always", "busy_start": "23:00", "busy_end": "01:00"}, ROOT / "prompts")
check(not schedule.state_at(stamp(0)).online and schedule.state_at(stamp(0)).activity == "mission", "cross midnight busy")
check(schedule.state_at(stamp(1)).online, "busy end excluded")
check(schedule.online_since(stamp(2)) == stamp(1), "busy splits online start")
check(schedule.offline_after(stamp(2)) == stamp(23), "busy splits online end")
for config in ({"online_start": "25:00"}, {"busy_start": "10:00"},
               {"busy_start": "10:00", "busy_end": "10:00"}, {"schedule_mode": "bad"}, {"character_name": "a\nb"}):
    before = profile.schedule_settings()
    try:
        profile.configure(config, ROOT / "prompts")
    except ValueError:
        check(before == profile.schedule_settings(), "invalid config keeps active snapshot")
    else:
        raise AssertionError("invalid config accepted")

custom = profile.prepare({"character_name": "阿澄", "primary_user_name": "小雨"}, ROOT / "prompts")
custom["voice"] = '【口吻】{character_name}说话简洁，例子JSON {"a":1}。'
profile.apply(custom)
check("阿澄" in persona.VOICE and '{"a":1}' in persona.VOICE, "dynamic voice and literal braces")
check(winext.TRAILBLAZER_NAME == "小雨", "dynamic compatibility primary name")
check("阿澄" in winext.window_judge_system("书友群", "在线", "有精神"), "judge uses configured persona")
check("@阿澄" in winext.group_block("书友群", "demo-user"), "mentions configured name")
check("小雨" in winext.mission_ack_context("忙碌"), "busy acknowledgement primary label")
check("你是阿澄" in persona.build_consolidate_system([], []) and "小雨" in persona.build_consolidate_system([], []), "memory identity configurable")
check("阿澄" in winext.group_consolidate_system("书友群", [], []), "group memory identity configurable")
check("【说话方式】" not in persona.VOICE, "edited voice replaces sample fully")
with patch("pathlib.Path.read_text", return_value=""):
    try:
        profile.configure({"character_name": "错误"}, ROOT / "prompts")
    except ValueError:
        check(profile.bot_name() == "阿澄", "empty prompt keeps profile")
    else:
        raise AssertionError("empty prompt accepted")
custom["character_name"] = "changed after apply"
check(profile.bot_name() == "阿澄", "apply copies input")
a = profile.prepare({"character_name": "角色甲", "schedule_mode": "always"}, ROOT / "prompts")
b = profile.prepare({"character_name": "角色乙", "online_start": "20:00", "online_end": "04:00"}, ROOT / "prompts")
with profile.use(a):
    check(profile.bot_name() == "角色甲" and schedule.state_at(stamp(12)).online, "context A schedule and name")
    with profile.use(b):
        check(profile.bot_name() == "角色乙" and not schedule.state_at(stamp(12)).online, "context B schedule and name")
    check(profile.bot_name() == "角色甲", "nested context restored")
check(profile.bot_name() == "阿澄", "global profile restored")


async def contexts():
    gate = asyncio.Event()

    async def read():
        await gate.wait()
        return profile.bot_name(), schedule.state_at(stamp(12)).online

    with profile.use(a):
        ta = asyncio.create_task(read())
    with profile.use(b):
        tb = asyncio.create_task(read())
    a["character_name"] = "later mutation"
    gate.set()
    return await asyncio.gather(ta, tb)


check(asyncio.run(contexts()) == [("角色甲", True), ("角色乙", False)], "spawned tasks retain isolated copied context")
profile.configure({}, ROOT / "prompts")
assembled = "\n".join([persona.build_system_prompt(), persona.build_consolidate_system([], []),
    persona.build_haze_system(False), persona.build_first_contact_context(),
    winext.peer_block("友人", "demo-user", "普通朋友", True), winext.group_block("书友群", "demo-primary"),
    winext.group_consolidate_system("书友群", [], []), winext.window_judge_system("书友群", "在线", "有精神")])
for i, bad in enumerate(["流萤", "开拓者", "星核猎手", "银狼", "匹诺康尼", "格拉默", "信用点", "很深的羁绊", "已有在意的人"]):
    check(bad not in assembled, f"no fixed setting {i + 1}")
check(persona.build_self_block("## 给角色的\n喜欢读书。\n## 出处\n虚构。") is not None, "generic self heading")
for filename in ["profile.py", "persona.py", "winext.py", "schedule.py"]:
    ast.parse((ROOT / "multichat" / filename).read_text(encoding="utf-8-sig"))
    check(True, "syntax " + filename)

# Runtime instances must not borrow the global self-document or one another's.
persona.load_self("## 给角色的\nGLOBAL_SELF_MARKER\n## 出处")
a["self_block"] = "ONLY_A_SELF_MARKER"
b["self_block"] = None
with profile.use(a):
    check("ONLY_A_SELF_MARKER" in persona.build_system_prompt() and "GLOBAL_SELF_MARKER" not in persona.build_system_prompt(), "runtime self overrides legacy global")
with profile.use(b):
    check("ONLY_A_SELF_MARKER" not in persona.build_system_prompt() and "GLOBAL_SELF_MARKER" not in persona.build_system_prompt(), "empty runtime self remains empty")
persona.load_self(None)
print(f"profile {passed}/{passed}")
