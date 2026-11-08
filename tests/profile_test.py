"""Offline persona/schedule contract checks. No runtime data or model calls."""
import ast
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from multichat import profile, persona, winext, schedule, identity

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
FEWER = "**说得少，是少说几件事、少发几条，不是把每句话削短。**每句话该有的语气和衔接要留着。"
check(FEWER in persona.FORMAT and FEWER in persona.build_system_prompt()
      and persona.FORMAT.index("每条单独占一行。**") < persona.FORMAT.index(FEWER) < persona.FORMAT.index("只看本轮要回应的内容"),
      "saying less means fewer messages, not clipped sentences")
special = [persona.build_standalone_farewell_context(7.5), persona.build_recheck_context("", True, False, "5分钟", "a", 40),
           persona.build_recheck_context("话题", False, False, "5分钟", "a", 20), persona.build_recheck_context("话题", False, True, "刚才", "a", 0.3),
           persona.build_proactive_context("a", True, 60.4, True), persona.build_proactive_context("b", False, 9, False)]
check(all("话不多" in t and "说完整" in t and not any(x in t for x in ("条，短。", "条，短：", "条，短，")) for t in special)
      and "只说 1～2 条" in special[0] and all("只说 1～3 条" in t for t in special[4:]),
      "special cases keep message caps but ask for complete sentences instead of bare 'short'")
from multichat import farewell as fw
follow = fw.followup_system("测试身份", now=stamp(10))
check("一两句话即可；话不多，但每句说完整。" in follow and "简短的聊天消息" not in follow and "不要展开新话题" in follow,
      "final follow-up stays one message but complete")
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
    check("ONLY_A_SELF_MARKER" not in persona.build_system_prompt() and "GLOBAL_SELF_MARKER" not in persona.build_system_prompt(), "runtime detailed self is not resident")
    check(persona.identity_self() == "ONLY_A_SELF_MARKER", "runtime self overrides legacy global")
    detail = identity.query("ONLY_A_SELF_MARKER")
    check("ONLY_A_SELF_MARKER" in detail and "GLOBAL_SELF_MARKER" not in detail, "on-demand identity stays in runtime A")
with profile.use(b):
    check("ONLY_A_SELF_MARKER" not in persona.build_system_prompt() and "GLOBAL_SELF_MARKER" not in persona.build_system_prompt(), "empty runtime self remains empty")
    check(persona.identity_self() is None and "GLOBAL_SELF_MARKER" not in identity.query("GLOBAL_SELF_MARKER"), "on-demand empty runtime does not borrow global self")
persona.load_self(None)
print(f"profile {passed}/{passed}")
