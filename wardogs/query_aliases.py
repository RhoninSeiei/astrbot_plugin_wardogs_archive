"""Explicit player query aliases; bounded whole-message matching, never fuzzy IDs."""

import re
import unicodedata

from .ban_statistics import match_query as match_ban_statistics

# This table is also exported into the user documentation. Typos are opt-in.
ALIASES = {
    "population": (
        "人数",
        "在线人数",
        "服务器人数",
        "多少人",
        "几个人",
        "几人",
        "有多少人",
        "有几个人",
        "有几人",
        "多少人在线",
        "有多少人在线",
        "在线多少人",
        "在线几人",
        "空位",
        "位置",
        "还有空位",
        "还有位置",
        "有空位",
        "有位置",
        "有坑",
        "有坑位",
        "还有坑",
        "还有坑位",
        "有空位没",
        "有空位没有",
        "还有坑位没",
        "还有位置没",
        "满员",
        "满人",
        "满服",
        "服满",
        "服满没",
        "人满",
        "满员没",
        "满人没",
        "满服没",
        "人满没",
        "人多不多",
        "人多",
        "rs",
        "人输",
        "人树",
        "在现人数",
        "在线人输",
        "在线人树",
    ),
    "score": (
        "比分",
        "得分",
        "分数",
        "票数",
        "战况",
        "几比几",
        "多少分",
        "打几比几",
        "谁领先",
        "哪队领先",
        "哪边领先",
        "红蓝绿多少分",
        "打成什么样",
        "打成啥样",
        "打得怎么样",
        "打得咋样",
        "bf",
        "比份",
        "比芬",
    ),
    "both": (
        "状况",
        "状态",
        "情况",
        "服况",
        "什么情况",
        "人数比分",
        "比分人数",
        "人数战况",
        "战况人数",
        "服务器咋样",
        "服咋样",
        "服里啥情况",
        "zt",
        "fwqzt",
        "状太",
        "装态",
        "服务器状太",
        "服务器装态",
    ),
    "help": (
        "帮助",
        "查询帮助",
        "查服帮助",
        "菜单",
        "指令",
        "查询命令",
        "help",
        "怎么查人数",
        "怎么查比分",
        "怎么查服",
        "能查啥",
        "能查什么",
        "有什么查询命令",
        "人数怎么查",
        "比分怎么查",
    ),
}

HELP = "普通成员可@机器人查询：人数（在线人数与上限）、比分（当前各队得分）、状况（人数、比分与服务器ID）。服务器ID可说“服务器id”“服id”“服地址”“服务器地址”，从游戏左下角“ID加入”进入。也可说“还有空位吗”“得分怎么样了”“服务器状况”；也可查询“封禁统计”“过去24小时封禁人数”“过去12小时封禁人数”；输入“查询帮助”查看用法。"
TRANSLATION = str.maketrans(
    {
        "現": "现",
        "幾": "几",
        "個": "个",
        "數": "数",
        "線": "线",
        "態": "态",
        "況": "况",
        "務": "务",
        "戰": "战",
        "誰": "谁",
        "隊": "队",
        "領": "领",
        "樣": "样",
        "麼": "么",
        "嗎": "吗",
        "還": "还",
        "滿": "满",
        "員": "员",
        "請": "请",
        "問": "问",
        "幫": "帮",
        "謝": "谢",
        "詢": "询",
        "單": "单",
        "這": "这",
        "兒": "儿",
        "輸": "输",
        "與": "与",
        "狀": "状",
        "贏": "赢",
        "別": "别",
        "說": "说",
        "轉": "转",
        "發": "发",
        "歷": "历",
        "啟": "启",
        "關": "关",
        "設": "设",
        "調": "调",
        "裏": "里",
        "裡": "里",
        "確": "确",
        "認": "认",
        "報": "报",
        "廣": "广",
        "閉": "闭",
        "過": "过",
        "統": "统",
        "計": "计",
        "時": "时",
        "間": "间",
        "號": "号",
    }
)
# Do not remove these words during normalization: they change the request's meaning.
REJECT = re.compile(
    r"(?<![分特])别(?:再)?(?:查|看|报|问|发|说|回答|告诉|给)|不要|不用|不查|不看|不报|不需要|不是问|不是要|没问|没让|无需|撤回|取消|"
    r"他说|她说|有人说|有人(?:在)?问|(?:管理员|群主|管理|别人)(?:刚才)?(?:说|问)|转发|转述|引用|如果|示例|模拟|"
    r"群(?:里|内|在线|人数|成员|多少|几|满)|qq群|微信群|本群|这个群|昨天|昨晚|前天|上次|上周|上轮|上个月|去年|刚才|刚刚|前一局|上局|上一局|上把|上一把|上场|上一场|历史|之前|分钟前|"
    r"举报|补充|进度|确认|公告|广播|通知全服|重启|关服|停服|设置|修改|怎么改|调整|"
    r"禁言|发布通知|发通知|踢|封|t人|t掉|kick|\bban\b|查询玩家|查询昵称|足球|篮球|英超|nba|"
    r"[\"'“”「」『』]",
    re.I,
)
TRIM = "，,。.!！?？~～、:：；;"
PREFIX = re.compile(
    r"^(?:请问|麻烦|帮忙|请|问一下|问下|查询|查一下|查下|查查|看看|看一下|看下|报一下|报下|报个|给个|查|看|报|现在|当前|目前|这会儿|这会|这把|这局|本局|对局|服务器|伺服器|服务端|服里|服内)"
)
PARTICLE = re.compile(r"(?:呢|啊|呀|呗|嘛|吗|么|啦|了)$")
END_QUERY = re.compile(
    r"^(比分|得分|分数|票数|战况|比份|比芬)(?:是|有)?(?:多少(?:分)?|怎么样|咋样|如何|什么情况|几比几)$"
)
JOIN = re.compile(r"(?:以及|顺便|还有|和|及|与|跟|并|[,，、;+＋/])+")


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold().translate(TRANSLATION)
    text = re.sub(r"^/?wd(?=\s|[\u4e00-\u9fff])\s*", "", text.strip().removeprefix("/"))
    text = re.sub(r"[\s\u200b\ufeff]+", "", text).strip(TRIM)
    return text


def single(text):
    # Only bounded prefixes and sentence-final particles are optional.
    for _ in range(3):
        text = PARTICLE.sub("", text)
    for _ in range(8):
        text = text.strip(TRIM)
        for kind, values in ALIASES.items():
            if text in values:
                return kind
        changed = PREFIX.sub("", text, count=1)
        if changed == text:
            break
        text = changed
    if END_QUERY.fullmatch(text):
        return "score"
    if re.fullmatch(r"(?:状态|状况|服况|情况|状太|装态)(?:怎么样|咋样|如何)", text):
        return "both"
    if re.fullmatch(r"(?:在线)?(?:人数|人输|人树)(?:是|有)?(?:多少|几人|几个)", text):
        return "population"
    return None


def local_query(text):
    """Return a fixed kind, 'none' to decline, or None for existing LLM fallback."""
    value = normalized(text)
    # Help phrases intentionally contain 怎么, but never administration actions.
    if value in ALIASES["help"]:
        return "help"
    stats_kind = match_ban_statistics(value)
    if stats_kind is not None:
        return stats_kind
    if REJECT.search(value):
        return "none"
    if not value or len(value) > 160:
        return None
    for _ in range(2):
        value = re.sub(r"(?:谢谢|谢了|谢谢啦)$", "", value).strip(TRIM)
    if (
        value in ALIASES["both"]
        or value in ALIASES["score"]
        or value in ALIASES["population"]
    ):
        return next(kind for kind, values in ALIASES.items() if value in values)
    parts = JOIN.split(value)
    # 还有 belongs to population questions unless an actual combination matches.
    if 1 < len(parts) <= 3 and all(parts):
        kinds = [single(part) for part in parts]
        if all(kind in ("population", "score", "both") for kind in kinds):
            return "both" if "both" in kinds or len(set(kinds)) > 1 else kinds[0]
    return single(value)
