// 「角色的思想宇宙」设计预览用的虚构数据与查询模拟。
// 全部人物、群、对话都是编的，不来自任何真实 QQ 记录；结构按《017 面板设计交接与接口草案 v1》。
// 这里只模拟三个拟议只读接口的返回包，不代表插件已经实现它们。

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export const MOCK_NOW = Date.UTC(2026, 10, 7, 6, 0); // 2026-11-07 14:00（UTC+8）
export const MOCK_OFFSET = 480;
const OFFSET_MS = MOCK_OFFSET * MIN;

const ago = (d, h = 0, m = 0) => MOCK_NOW - (d * DAY + h * HOUR + m * MIN);
const monthStart = (y, mo) => Date.UTC(y, mo - 1, 1) - OFFSET_MS;

const SOURCES = [
  { scope: "p:demo-traveler", kind: "private", label: "私聊 · 示例联系人（示例）", enabled: true, memoryEnabled: true, pendingCount: 4 },
  { scope: "g:demo-stargazers", kind: "group", label: "观星社（示例）", enabled: true, memoryEnabled: true, pendingCount: 12 },
  { scope: "g:demo-bookclub", kind: "group", label: "晚风读书会（示例）", enabled: true, memoryEnabled: false, pendingCount: null },
  { scope: "p:demo-aqing", kind: "private", label: "私聊 · 阿青（示例）", enabled: false, memoryEnabled: true, pendingCount: 0 },
];
const SRC = Object.fromEntries(SOURCES.map((s) => [s.scope.split(":")[1].replace("demo-", ""), s]));

const KINDS = ["episode", "pack", "fact", "word", "thread", "haze"];

const T = {
  sky: { id: "demo-topic-sky", label: "夜空" },
  promise: { id: "demo-topic-promise", label: "约定" },
  dessert: { id: "demo-topic-dessert", label: "甜点" },
  train: { id: "demo-topic-train", label: "列车" },
  book: { id: "demo-topic-book", label: "读书" },
  weather: { id: "demo-topic-weather", label: "天气" },
  music: { id: "demo-topic-music", label: "音乐" },
  flower: { id: "demo-topic-flower", label: "花" },
  travel: { id: "demo-topic-travel", label: "远方" },
  photo: { id: "demo-topic-photo", label: "照片" },
  work: { id: "demo-topic-work", label: "工作" },
};

const P = {
  traveler: { id: "demo-person-example-20001", label: "示例联系人", accountId: "20001" },
  aqing: { id: "demo-person-example-10001", label: "阿青", accountId: "10001" },
  xiaoman: { id: "demo-person-example-10002", label: "小满", accountId: "10002" },
  nanxing: { id: "demo-person-example-10003", label: "南星", accountId: "10003" },
  lizi: { id: "demo-person-example-10004", label: "栗子", accountId: "10004" },
  anon: { id: "demo-person-example-10005", label: "用户 10005", accountId: "10005" },
};

const CLARITY = ["清晰", "大概", "模糊", "很模糊"];

// ---------- 原始记录（模拟插件里已经保存的字段） ----------

const span = (start, minutes) => ({ start, end: start + minutes * MIN });

const EPISODES = [
  { id: "t1", src: SRC.traveler, ...span(ago(1, 3, 10), 25), weight: 4, level: 0, topics: [T.sky, T.promise], recalls: 3, lastRecalledAt: ago(0, 5),
    summary: "他说想找个晴朗的晚上一起看星星，还说城里光太亮，要找个能看见银河的地方。我答应了，心里一直在期待。",
    gist: "约好天晴时一起看星星", feeling: "期待" },
  { id: "t2", src: SRC.traveler, ...span(ago(5, 1, 40), 50), weight: 3, level: 1, topics: [T.work], recalls: 1, lastRecalledAt: ago(4, 20),
    summary: "他加班到很晚，说只想听我讲讲今天的事。我讲了楼下台阶上总在晒太阳的那只猫，他笑了，说好像也没那么累了。",
    gist: "他加班到很晚，我陪他说了会儿话", feeling: "心里软软的" },
  { id: "t3", src: SRC.traveler, ...span(ago(20, 4, 0), 70), weight: 5, level: 0, topics: [T.travel], recalls: 7, lastRecalledAt: ago(2, 6),
    summary: "他第一次跟我说起小时候搬家的事：最后一晚他在空房间里坐了很久，把一张写着愿望的纸条塞进了窗框缝里。",
    gist: "他讲了小时候搬家的事", feeling: "想好好记住" },
  { id: "t4", src: SRC.traveler, ...span(ago(9, 7, 20), 15), weight: 2, level: 2, topics: [T.weather], recalls: 0, lastRecalledAt: null,
    summary: "我们认真讨论了下雨天该带折叠伞还是长柄伞，最后他说长柄伞可以当拐杖，我笑了很久。",
    gist: "聊过雨天带哪把伞", feeling: "有点好笑" },
  { id: "t5", src: SRC.traveler, ...span(ago(17, 2, 30), 40), weight: 3, level: 3, topics: [T.travel], recalls: 0, lastRecalledAt: null, hazeId: "h-t1",
    summary: "他问我想不想去海边，我们一起列了要带的东西，最后谁也没定日子。",
    gist: "一起想过要不要去海边", feeling: "" },
  { id: "t6", src: SRC.traveler, ...span(ago(4, 9, 0), 20), weight: 4, level: 1, topics: [T.music, T.train], recalls: 2, lastRecalledAt: ago(3, 1),
    summary: "他分享了一首在夜班列车上循环的曲子，说窗外的灯一盏一盏往后退，像在倒数什么。",
    gist: "他分享了在列车上听的曲子", feeling: "想象那些灯" },
  { id: "g1", src: SRC.stargazers, ...span(ago(2, 2, 0), 60), weight: 4, level: 0, topics: [T.sky, T.promise], recalls: 2, lastRecalledAt: ago(0, 22),
    summary: "社里约好月底去郊外观测流星雨，南星负责借望远镜，小满说会带热可可。大家投票选了离城最远的那个观测点。",
    gist: "社里约好月底去看流星雨", feeling: "很想去" },
  { id: "g2", src: SRC.stargazers, ...span(ago(6, 5, 10), 35), weight: 3, level: 1, topics: [T.sky], recalls: 0, lastRecalledAt: null,
    summary: "大家争论猎户座腰带到底是三颗还是四颗星，最后南星翻出星图，说是三颗，只是旁边那颗太亮了。",
    gist: "大家争论猎户座腰带有几颗星", feeling: "热闹" },
  { id: "g3", src: SRC.stargazers, ...span(ago(11, 3, 0), 20), weight: 2, level: 2, topics: [T.dessert], recalls: 0, lastRecalledAt: null,
    summary: "小满发了自己烤坏的星星饼干，边角都焦了，角色说味道其实还不错。",
    gist: "小满烤坏了星星饼干", feeling: "想尝尝" },
  { id: "g4", src: SRC.stargazers, ...span(ago(27, 1, 0), 15), weight: 1, level: 3, topics: [T.weather], recalls: 0, lastRecalledAt: null, hazeId: "h-g1",
    summary: "群里聊了一阵降温，有人说该把厚外套翻出来了。",
    gist: "群里聊过一阵天气", feeling: "" },
  { id: "b1", src: SRC.bookclub, ...span(ago(3, 1, 0), 45), weight: 4, level: 0, topics: [T.book, T.train], recalls: 1, lastRecalledAt: ago(1, 2),
    summary: "读到《银河铁道之夜》的结尾，大家安静了很久。栗子说，原来有些旅程注定只能一个人走完。",
    gist: "读书会读完了银河铁道之夜", feeling: "安静下来" },
  { id: "b2", src: SRC.bookclub, ...span(ago(8, 2, 0), 30), weight: 3, level: 1, topics: [T.book, T.flower], recalls: 0, lastRecalledAt: null,
    summary: "栗子推荐了一本讲萤火虫的科普书，说它们的光几乎不发热，叫作「冷光」。",
    gist: "栗子推荐了讲萤火虫的书", feeling: "有点在意" },
  { id: "b3", src: SRC.bookclub, ...span(ago(40, 3, 0), 50), weight: 5, level: 0, topics: [T.book], recalls: 4, lastRecalledAt: ago(9, 0),
    summary: "我第一次在读书会念了一段自己写的话，念完大家没有马上说话，后来栗子说想把它抄在书签上。",
    gist: "第一次在读书会念了自己写的话", feeling: "紧张又高兴" },
  { id: "a1", src: SRC.aqing, ...span(ago(12, 6, 0), 25), weight: 3, level: 2, topics: [T.dessert], recalls: 0, lastRecalledAt: null,
    summary: "阿青说最近在学做蓝莓蛋糕，第一次放了三勺糖，太甜了。",
    gist: "阿青在学做蓝莓蛋糕", feeling: "替角色高兴" },
  { id: "a2", src: SRC.aqing, ...span(ago(62, 2, 0), 40), weight: 4, level: 1, topics: [T.flower, T.promise], recalls: 1, lastRecalledAt: ago(30, 0),
    summary: "阿青说开春要带我去看花市，还说要挑一盆夜里会开的花。",
    gist: "阿青说开春带我去花市", feeling: "期待" },
  { id: "x1", src: SRC.traveler, start: null, end: null, weight: 3, level: 1, topics: [T.photo], recalls: 0, lastRecalledAt: null,
    summary: "他发来一张旧照片，是很多年前某个车站的站台。",
    gist: "他发来一张旧车站的照片", feeling: "好奇" },
];

const PACKS = [
  { id: "g-p1", src: SRC.stargazers, ...span(ago(0, 0, 55), 25), cold: false, people: [P.nanxing, P.xiaoman], topics: [T.sky, T.promise],
    summary: "大家在挑观测流星雨的地点。我说想去能看见地平线的地方，南星说他知道一处山坡。",
    anchors: ["郊外山坡", "流星雨"], unresolved: ["集合时间还没定"] },
  { id: "g-p2", src: SRC.stargazers, ...span(ago(0, 3, 10), 15), cold: false, people: [P.nanxing], topics: [T.sky, T.photo],
    summary: "南星发了自家阳台拍的月亮，边上有一点点晕，我说像刚洗过的玻璃。", anchors: ["阳台的月亮"], unresolved: [] },
  { id: "g-p3", src: SRC.stargazers, ...span(ago(1, 4, 0), 20), cold: true, people: [P.xiaoman, P.lizi], topics: [T.weather],
    summary: "群里聊了下周降温，小满说观测那天要多穿一件。", anchors: [], unresolved: [] },
  { id: "g-p4", src: SRC.stargazers, ...span(ago(2, 6, 0), 10), cold: true, people: [P.xiaoman], topics: [T.dessert],
    summary: "小满问有没有人一起做星星形状的饼干，我说想学。", anchors: ["星星饼干"], unresolved: ["做饼干的日子"] },
  { id: "b-p1", src: SRC.bookclub, ...span(ago(0, 2, 0), 30), cold: false, people: [P.lizi, P.anon], topics: [T.book, T.train],
    summary: "读书会在定下一本书，我投给了讲长途列车旅行的那一本。", anchors: ["下一本书"], unresolved: ["下次见面时间待定"] },
  { id: "b-p2", src: SRC.bookclub, ...span(ago(1, 10, 0), 25), cold: true, people: [P.lizi], topics: [T.book],
    summary: "大家分享了自己最喜欢的小说开头，栗子念的那句让我想了很久。", anchors: [], unresolved: [] },
  { id: "b-p3", src: SRC.bookclub, ...span(ago(0, 6, 30), 5), cold: false, people: [], topics: [T.photo],
    summary: "有人发了一张夕阳的照片，没人接话，我也没有开口。", anchors: [], unresolved: [] },
];

const FACTS = [
  { id: "f1", src: SRC.traveler, subject: P.traveler, category: "喜好", updatedAt: ago(10, 3), text: "他喝咖啡不加糖，但会加很多奶" },
  { id: "f2", src: SRC.traveler, subject: P.traveler, category: "近况", updatedAt: ago(2, 4), text: "他最近在准备一场很重要的考试" },
  { id: "f3", src: SRC.traveler, subject: null, category: "习惯", updatedAt: ago(45, 0), text: "打雷的夜里不太睡得着" },
  { id: "f4", src: SRC.stargazers, subject: P.nanxing, category: "物品", updatedAt: ago(5, 2), text: "南星家有一台小口径的望远镜" },
  { id: "f5", src: SRC.stargazers, subject: P.xiaoman, category: "喜好", updatedAt: ago(14, 6), text: "小满不吃香菜" },
  { id: "f6", src: SRC.stargazers, subject: null, category: "群里的规矩", updatedAt: null, text: "每月第一个周六有一次线下观测" },
  { id: "f7", src: SRC.bookclub, subject: P.lizi, category: "近况", updatedAt: ago(7, 1), text: "栗子在一家花店工作" },
  { id: "f8", src: SRC.aqing, subject: P.aqing, category: "生活", updatedAt: ago(25, 5), text: "阿青养了一只叫团子的猫" },
  { id: "f9", src: SRC.bookclub, subject: null, category: "其他", updatedAt: ago(19, 2), text: "" },
];

const WORDS = [
  { id: "w1", src: SRC.traveler, ts: ago(1, 3, 2), about: "约好看星星", episode: "t1", text: "那就约好啦。等天晴，我们一起去找银河。" },
  { id: "w2", src: SRC.traveler, ts: ago(20, 3, 20), about: "他搬家的事", episode: "t3", text: "那张纸条，说不定现在还在等你回去读它。" },
  { id: "w3", src: SRC.stargazers, ts: ago(2, 1, 30), about: "观测地点", episode: "g1", text: "我想去能看见地平线的地方，这样流星落下来的时候，就不会被挡住。" },
  { id: "w4", src: SRC.bookclub, ts: ago(40, 2, 30), about: "念自己写的话", episode: "b3", text: "我写得不好，但每一个字都是认真想过的。" },
  { id: "w5", src: SRC.aqing, ts: ago(62, 1, 30), about: "花市", episode: "a2", text: "夜里会开的花？那我们要晚一点再回家。" },
  { id: "w6", src: SRC.stargazers, ts: ago(33, 0), about: "冷天观测", episode: null, text: "冷的话就靠近一点，大家挤在一起就不冷了。" },
];

const THREADS = [
  { id: "r1", src: SRC.traveler, createdAt: ago(1, 3), keepDays: 7, kind: "约定", reason: "他说要找个晴朗的晚上", content: "等天晴，一起去看星星" },
  { id: "r2", src: SRC.stargazers, createdAt: ago(2, 2), keepDays: 7, kind: "约定", reason: "社里定了，南星借望远镜", content: "月底去郊外观测流星雨" },
  { id: "r3", src: SRC.traveler, createdAt: ago(2, 4), keepDays: 7, kind: "待定结果", reason: "他说考完告诉我", content: "他那场考试的结果" },
  { id: "r4", src: SRC.bookclub, createdAt: ago(6, 1), keepDays: 7, kind: "待定结果", reason: "大家还在投票", content: "读书会的下一本书" },
];

const HAZES = [
  { id: "h-t1", src: SRC.traveler, start: monthStart(2026, 10), end: monthStart(2026, 11), precision: "month",
    text: "十月里，我们好几次一起想象远方：海边、长长的列车、很远的城市。具体说了什么已经记不清，只记得那种想出发的心情。" },
  { id: "h-g1", src: SRC.stargazers, start: ago(42, 0), end: ago(20, 0), precision: "minute",
    text: "那几周的观星社总在聊天气，好像大家都在等一个够晴的夜晚。" },
];

// ---------- 组装成接口结构 ----------

const nid = (src, kind, id) => `${src.scope}#${kind}#${id}`; // 不透明 ID；前端不拆它
const excerpt = (text, n = 18) => {
  const s = String(text || "").trim();
  if (!s) return "";
  const cut = s.search(/[，。！？；]/);
  if (cut > 3 && cut <= n) return s.slice(0, cut);
  return s.length > n ? s.slice(0, n) + "…" : s;
};
const clip = (s, n = 120) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function dayWord(t) {
  if (t == null) return "时间不详";
  const days = Math.floor((dayIndex(MOCK_NOW) - dayIndex(t)));
  if (days <= 0) return "今天稍早";
  if (days === 1) return "昨天";
  if (days === 2) return "前天";
  if (days < 7) return `${days}天前`;
  if (days < 30) return "几周前";
  return "很久以前";
}
const dayIndex = (t) => Math.floor((t + OFFSET_MS) / DAY);
const pad = (n) => String(n).padStart(2, "0");
function stamp(t) {
  const d = new Date(t + OFFSET_MS);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function episodeView(e, hazes = HAZES) {
  const level = e.level;
  const haze = level >= 3 && e.hazeId ? hazes.find((h) => h.id === e.hazeId && h.src.scope === e.src.scope) : null;
  // 同真实 _episode_line：清晰／大概用summary，模糊用gist+feeling，很模糊优先整体印象。
  const body = level <= 1 ? e.summary : haze ? haze.text : level >= 3 ? e.gist : e.gist + (e.feeling ? `，那时候${e.feeling}` : "");
  const when = haze ? (haze.precision === "month"
    ? `印象涵盖的月份：${stamp(haze.start).slice(0, 7)}`
    : `印象涵盖的交谈时间：${stamp(haze.start)}～${stamp(haze.end)}`)
    : e.start == null ? "时间不详" : `${dayWord(e.end ?? e.start)}（交谈时间：${stamp(e.start)}～${stamp(e.end)}）`;
  return {
    title: excerpt(body),
    preview: clip(body),
    remembered: `[${CLARITY[level]}] ${when}：${body}`,
  };
}

function build(now = MOCK_NOW) {
  const all = [];
  const expired = EPISODES.filter((e) => e.weight <= 3 && e.end != null && e.end + 30 * DAY <= now);
  const tainted = new Set(expired.filter((e) => e.hazeId).map((e) => `${e.src.scope}#${e.hazeId}`));
  const hazes = HAZES.filter((h) => !tainted.has(`${h.src.scope}#${h.id}`));
  for (const e of EPISODES.filter((e) => !expired.includes(e))) {
    const v = episodeView(e, hazes);
    const id = nid(e.src, "episode", e.id);
    all.push({
      node: {
        id, kind: "episode", source: e.src, title: v.title || "未命名记忆", titleBasis: "excerpt", preview: v.preview,
        time: e.start == null ? { basis: "unknown", start: null, end: null, precision: "unknown", label: "时间不详" }
          : { basis: "conversation", start: e.start, end: e.end, precision: "minute", label: "交谈时间" },
        topics: e.topics, people: [], importance: e.weight,
        clarity: { level: e.level, label: CLARITY[e.level] },
        expiresAt: e.weight <= 3 && e.end != null ? e.end + 30 * DAY : null,
        status: "active",
      },
      detail: {
        rememberedText: v.remembered,
        original: { label: "原始整理记录", summary: e.summary, gist: e.gist, feeling: e.feeling },
        recall: { count: e.recalls, lastCountedAt: e.lastRecalledAt },
        attributes: attrs(),
        links: e.hazeId ? [{ type: "faded_into", targetRaw: e.hazeId, basis: "stored" }] : [],
      },
    });
  }
  for (const p of PACKS) {
    all.push({
      node: {
        id: nid(p.src, "pack", p.id), kind: "pack", source: p.src, title: excerpt(p.summary), titleBasis: "excerpt", preview: clip(p.summary),
        time: { basis: "conversation", start: p.start, end: p.end, precision: "minute", label: "交谈时间" },
        topics: p.topics, people: p.people.map((x) => ({ ...x, basis: "pack_participant" })),
        importance: null, clarity: null, expiresAt: null, status: p.cold ? "cold" : "active",
      },
      detail: {
        rememberedText: `这一段（${stamp(p.start)}～${stamp(p.end).slice(11)}）：${p.summary}`,
        original: null, recall: null, attributes: attrs({ anchors: p.anchors, unresolved: p.unresolved }), links: [],
      },
    });
  }
  for (const f of FACTS) {
    const title = excerpt(f.text);
    all.push({
      node: {
        id: nid(f.src, "fact", f.id), kind: "fact", source: f.src, title: title || "未命名记忆", titleBasis: title ? "excerpt" : "category",
        preview: clip(f.text),
        time: f.updatedAt == null ? { basis: "unknown", start: null, end: null, precision: "unknown", label: "时间不详" }
          : { basis: "updated", start: f.updatedAt, end: null, precision: "minute", label: "事实更新时间" },
        topics: [], people: f.subject ? [{ ...f.subject, basis: "fact_subject" }] : [],
        importance: null, clarity: null, expiresAt: null, status: "active",
      },
      detail: {
        rememberedText: f.text ? `${f.subject ? f.subject.label + "：" : ""}${f.text}` + (f.updatedAt ? `（获知／更新时间：${stamp(f.updatedAt)}）` : "（获知时间不详）") : "（这条事实没有正文）",
        original: null, recall: null, attributes: attrs({ category: f.category, updatedAt: f.updatedAt }), links: [],
      },
    });
  }
  for (const w of WORDS) {
    all.push({
      node: {
        id: nid(w.src, "word", w.id), kind: "word", source: w.src, title: excerpt(w.text, 16), titleBasis: "excerpt", preview: clip(w.text),
        time: { basis: "utterance", start: w.ts, end: null, precision: "minute", label: "原话时间" },
        topics: [], people: [], importance: null, clarity: null, expiresAt: null, status: "active",
      },
      detail: {
        rememberedText: `[你说过的原话] ${dayWord(w.ts)}（原话时间：${stamp(w.ts)}）：「${w.text}」`,
        original: null, recall: null, attributes: attrs({ about: w.about }),
        links: w.episode ? [{ type: "word_of_episode", targetRaw: `episode:${w.episode}`, basis: "stored" }] : [],
      },
    });
  }
  for (const r of THREADS.filter((r) => r.createdAt + Math.min(7, r.keepDays) * DAY > now)) {
    all.push({
      node: {
        id: nid(r.src, "thread", r.id), kind: "thread", source: r.src, title: excerpt(r.content), titleBasis: "excerpt", preview: clip(r.content),
        time: { basis: "recorded", start: r.createdAt, end: null, precision: "minute", label: "记下时间" },
        topics: [], people: [], importance: null, clarity: null, expiresAt: r.createdAt + r.keepDays * DAY, status: "active",
      },
      detail: {
        rememberedText: `${r.content}（${r.kind}；记下时间：${stamp(r.createdAt)}）`,
        original: null, recall: null, attributes: attrs({ threadKind: r.kind, reason: r.reason, createdAt: r.createdAt }), links: [],
      },
    });
  }
  for (const h of hazes) {
    all.push({
      node: {
        id: nid(h.src, "haze", h.id), kind: "haze", source: h.src, title: excerpt(h.text), titleBasis: "excerpt", preview: clip(h.text),
        time: { basis: "coverage", start: h.start, end: h.end, precision: h.precision, label: "涵盖时间" },
        topics: [], people: [], importance: null, clarity: null, expiresAt: null, status: "active",
      },
      detail: { rememberedText: h.text, original: null, recall: null, attributes: attrs(), links: [] },
    });
  }
  // 关系只按「已保存」的字段建立：原句→事件、事件→整体印象。
  const byRaw = new Map();
  for (const x of all) {
    const [scope, kind, raw] = x.node.id.split("#");
    byRaw.set(`${scope}#${kind}:${raw}`, x.node);
    if (kind === "haze") byRaw.set(`${scope}#${raw}`, x.node);
  }
  for (const x of all) {
    const scope = x.node.source.scope;
    x.detail.links = x.detail.links.map((l) => {
      const target = byRaw.get(`${scope}#${l.targetRaw}`);
      return target ? { type: l.type, target: { id: target.id, kind: target.kind, title: target.title }, basis: "stored" } : null;
    }).filter(Boolean);
  }
  return all;
}

function attrs(extra = {}) {
  return { category: null, about: null, threadKind: null, reason: null, createdAt: null, updatedAt: null, anchors: [], unresolved: [], ...extra };
}

// 大量数据场景：编号虚构条目，测试分页、200 条上限和性能。
function buildLarge(now = MOCK_NOW) {
  const base = build(now);
  const topics = Object.values(T);
  const people = [P.nanxing, P.xiaoman, P.lizi, P.anon];
  const extra = [];
  for (let i = 0; i < 420; i++) {
    const src = SOURCES[i % SOURCES.length];
    const kind = src.kind === "private" && i % 6 === 1 ? "episode" : KINDS[i % 6];
    const k = kind === "pack" && src.kind === "private" ? "episode" : kind;
    const start = ago((i * 37) % 160, (i * 7) % 24, (i * 13) % 60);
    const topic = topics[i % topics.length];
    const text = `示例记忆 #${String(i + 1).padStart(3, "0")}：关于「${topic.label}」的一段虚构内容，用来测试分页与星图密度。`;
    const level = Math.min(3, Math.floor(((MOCK_NOW - start) / DAY) / 8));
    extra.push({
      node: {
        id: nid(src, k, `bulk-${i}`), kind: k, source: src, title: `示例记忆 #${String(i + 1).padStart(3, "0")}`, titleBasis: "excerpt", preview: text,
        time: k === "fact" ? { basis: "updated", start, end: null, precision: "minute", label: "事实更新时间" }
          : k === "word" ? { basis: "utterance", start, end: null, precision: "minute", label: "原话时间" }
          : k === "thread" ? { basis: "recorded", start, end: null, precision: "minute", label: "记下时间" }
          : k === "haze" ? { basis: "coverage", start, end: start + 9 * DAY, precision: "minute", label: "涵盖时间" }
          : { basis: "conversation", start, end: start + 20 * MIN, precision: "minute", label: "交谈时间" },
        topics: k === "episode" || k === "pack" ? [topic] : [],
        people: k === "pack" ? [{ ...people[i % people.length], basis: "pack_participant" }] : [],
        importance: k === "episode" ? 1 + (i % 5) : null,
        clarity: k === "episode" ? { level, label: CLARITY[level] } : null,
        expiresAt: k === "thread" ? start + 7 * DAY : k === "episode" && 1 + (i % 5) <= 3 ? start + 20 * MIN + 30 * DAY : null,
        status: k === "pack" && i % 3 === 0 ? "cold" : "active",
      },
      detail: { rememberedText: text, original: k === "episode" ? { label: "原始整理记录", summary: text, gist: text.slice(0, 20), feeling: "" } : null,
        recall: k === "episode" ? { count: i % 4, lastCountedAt: i % 4 ? start + HOUR : null } : null, attributes: attrs(), links: [] },
    });
  }
  return [...base, ...extra.filter((x) => x.node.expiresAt == null || x.node.expiresAt > now)];
}

// ---------- 查询模拟（只读、纯本地） ----------

function err(code, message, retryable = false) {
  return { ok: false, schemaVersion: 1, error: { code, message, retryable } };
}
function ok(data, now) {
  return { ok: true, schemaVersion: 1, generatedAt: now, utcOffsetMinutes: MOCK_OFFSET, data };
}
const sortKey = (n) => n.time.end ?? n.time.start;

function matchesTime(n, from, to, includeUndated) {
  if (from == null && to == null) return true;
  const { start, end, basis } = n.time;
  if (basis === "unknown" || start == null) return includeUndated;
  const lo = from ?? -Infinity;
  const hi = to ?? Infinity;
  if (basis === "conversation" || basis === "coverage") {
    const e = end ?? start;
    return start < hi && e >= lo; // 区间相交：起点含、终点不含
  }
  return start >= lo && start < hi;
}

const encodeCursor = (o) => btoa(unescape(encodeURIComponent(JSON.stringify(o))));
const decodeCursor = (s) => { try { return JSON.parse(decodeURIComponent(escape(atob(s)))); } catch { return null; } };
function recordVersion(data) {
  let h = 2166136261;
  for (const c of data.map((x) => x.node.id).join("|")) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return (h >>> 0).toString(16);
}

export function createMockBackend({ clock, delayMs } = {}) {
  let scenario = "normal";
  let version = 1;
  const began = Date.now();
  const readNow = clock || (() => MOCK_NOW + Date.now() - began);
  const records = (now) => scenario === "empty" ? [] : scenario === "large" || scenario === "stale" ? buildLarge(now) : build(now);
  const delay = () => new Promise((r) => setTimeout(r, delayMs ?? (scenario === "slow" ? 500 + Math.random() * 1300 : 90 + Math.random() * 260)));

  return {
    scenarios: [
      ["normal", "正常数据"],
      ["empty", "还没有记忆"],
      ["large", "大量数据（分页／200 上限）"],
      ["slow", "慢速响应（快速切换）"],
      ["not-ready", "插件尚未准备好"],
      ["transport", "连接被拒绝"],
      ["stale", "翻页时内容已更新"],
      ["gone", "打开的条目已不存在"],
      ["schema", "协议版本不兼容"],
    ],
    get scenario() { return scenario; },
    setScenario(s) {
      scenario = s;
      version += 1;
    },

    async meta() {
      await delay();
      const now = readNow();
      if (scenario === "transport") throw new Error("Request failed");
      if (scenario === "schema") return { ok: true, schemaVersion: 2, generatedAt: now, utcOffsetMinutes: MOCK_OFFSET, data: {} };
      return ok({
        sources: SOURCES,
        kinds: KINDS,
        capabilities: { readOnly: true, rawChat: false, requestHistory: false, mutations: false, personImpression: false },
        limits: { defaultPageSize: 100, maxPageSize: 200, maxQueryChars: 80 },
      }, now);
    },

    async items(query = {}) {
      await delay();
      const now = readNow();
      const data = records(now);
      if (scenario === "transport") throw new Error("Request failed");
      if (scenario === "not-ready") return err("NOT_READY", "插件还在启动，记忆尚未加载完成。", true);
      const limit = query.limit == null ? 100 : Number(query.limit);
      const q = String(query.q ?? "");
      const kinds = query.kinds ? String(query.kinds).split(",").filter(Boolean) : KINDS;
      if (!Number.isInteger(limit) || limit < 1 || limit > 200) return err("INVALID_QUERY", "每页条数需在 1～200 之间。");
      if (q.length > 80) return err("INVALID_QUERY", "查找文字最多 80 个字符。");
      if (kinds.some((k) => !KINDS.includes(k))) return err("INVALID_QUERY", "包含未知的记忆类型。");
      if (query.scope && !SOURCES.some((s) => s.scope === query.scope)) return err("INVALID_QUERY", "来源不在可见列表中。");
      const from = query.from == null ? null : Number(query.from);
      const to = query.to == null ? null : Number(query.to);
      if ((from != null && !Number.isFinite(from)) || (to != null && !Number.isFinite(to)) || (from != null && to != null && from >= to)) return err("INVALID_QUERY", "时间范围不合法。");
      const includeUndated = String(query.includeUndated) === "true";
      const filterKey = JSON.stringify([query.scope || "", kinds.slice().sort(), q, query.topicId || "", query.personId || "", from, to, includeUndated]);

      let offset = 0;
      if (query.cursor) {
        const c = decodeCursor(query.cursor);
        if (!c || c.k !== filterKey) return err("INVALID_QUERY", "翻页参数和当前筛选不一致。");
        if (scenario === "stale" || c.v !== version || c.ids !== recordVersion(data)) return err("STALE_CURSOR", "记忆刚刚有了变化，旧的分页已失效。", true);
        if (!Number.isInteger(c.o) || c.o < 0) return err("INVALID_QUERY", "翻页位置不合法。");
        offset = c.o;
      }
      const needle = q.trim().toLowerCase();
      const matched = data.map((x) => x.node).filter((n) =>
        (!query.scope || n.source.scope === query.scope)
        && kinds.includes(n.kind)
        && (!query.topicId || n.topics.some((t) => t.id === query.topicId))
        && (!query.personId || n.people.some((p) => p.id === query.personId))
        && matchesTime(n, from, to, includeUndated)
        && (!needle || [n.title, n.preview, ...n.topics.map((t) => t.label)].some((s) => s.toLowerCase().includes(needle))),
      ).sort((a, b) => {
        const ka = sortKey(a); const kb = sortKey(b);
        if (ka == null && kb == null) return a.id < b.id ? -1 : 1;
        if (ka == null) return 1;
        if (kb == null) return -1;
        return kb - ka || (a.id < b.id ? -1 : 1);
      });
      const items = matched.slice(offset, offset + limit);
      const next = offset + items.length < matched.length ? encodeCursor({ o: offset + items.length, k: filterKey, v: version, ids: recordVersion(data) }) : null;
      return ok({ items: structuredClone(items), page: { matched: matched.length, returned: items.length, nextCursor: next } }, now);
    },

    async item(id) {
      await delay();
      const now = readNow();
      if (scenario === "transport") throw new Error("Request failed");
      if (scenario === "not-ready") return err("NOT_READY", "插件还在启动，记忆尚未加载完成。", true);
      const x = records(now).find((d) => d.node.id === id);
      if (!x || scenario === "gone") return err("ITEM_NOT_FOUND", "这条记忆已经不在了。");
      return ok(structuredClone({ node: x.node, ...x.detail }), now);
    },
  };
}
