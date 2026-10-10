"""compose 分批自检：确认长课被切成多批、每批不超限，且切分不丢知识点/例题。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Cue, Example, FlowStep, KnowledgePoint, Lesson  # noqa: E402
from app.stages.compose import EXAMPLES_PER_CALL, KPS_PER_CALL, _chunk_kps  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def mk(n_kps: int, ex_per_kp: list[int]) -> list[KnowledgePoint]:
    return [
        KnowledgePoint(id=f"kp{i}", title=f"知识点{i}", ts_start=0.0, ts_end=100.0,
                       examples=[{"id": f"ex{i}-{j}", "title": f"例{i}-{j}"} for j in range(n)])
        for i, n in enumerate(ex_per_kp, 1)
    ]


print("=== 分批规则 ===")
print(f"  每批最多 {EXAMPLES_PER_CALL} 道例题 / {KPS_PER_CALL} 个知识点")

print("\n=== 用例 1：本次真实规模 9 个知识点、11 道例题 ===")
real = mk(9, [1, 2, 2, 1, 1, 1, 1, 1, 1])   # 合计 11
chunks = _chunk_kps(real)
print(f"  切成 {len(chunks)} 批：{[sum(len(k.examples) for k in c) for c in chunks]} 道")
check("知识点一个都没丢", sum(len(c) for c in chunks) == 9, f"{sum(len(c) for c in chunks)} 个")
check("例题一个都没丢", sum(len(k.examples) for c in chunks for k in c) == 11,
      f"{sum(len(k.examples) for c in chunks for k in c)} 道")
check("没有一批超例题上限",
      all(sum(len(k.examples) for k in c) <= EXAMPLES_PER_CALL for c in chunks),
      str([sum(len(k.examples) for k in c) for c in chunks]))
check("批数明显多于原来那一发（1 发 → %d 发）" % len(chunks), len(chunks) >= 3)

print("\n=== 用例 2：单个知识点就带 5 道例题 ===")
big = mk(2, [5, 1])
chunks = _chunk_kps(big)
print(f"  切成 {len(chunks)} 批：{[sum(len(k.examples) for k in c) for c in chunks]} 道")
check("不丢知识点", sum(len(c) for c in chunks) == 2)
check("不丢例题", sum(len(k.examples) for c in chunks for k in c) == 6)
check("例题多的那批单独成批（宁可一批大也不拆题）",
      any(sum(len(k.examples) for k in c) == 5 for c in chunks))

print("\n=== 用例 3：没有任何例题 ===")
none_ex = mk(3, [0, 0, 0])
chunks = _chunk_kps(none_ex)
check("无例题时也返回非空分批（骨架阶段要用）", len(chunks) >= 1, f"{len(chunks)} 批")

print("\n=== 用例 4：知识点数量超限但例题很少 ===")
many = mk(7, [1] * 7)
chunks = _chunk_kps(many)
print(f"  切成 {len(chunks)} 批，例题数 {[sum(len(k.examples) for k in c) for c in chunks]}")
check("例题没丢", sum(len(k.examples) for c in chunks for k in c) == 7)
check("知识点没丢", sum(len(c) for c in chunks) == 7)

print("\n=== 提示词可格式化 ===")
from app.llm.prompts import COMPOSE_EXAMPLES_USER, COMPOSE_OUTLINE_USER  # noqa: E402
from app.models import LessonMeta  # noqa: E402

meta = LessonMeta(source_title="测试课", uploader="UP", subject="数学", grade="高一",
                  video_duration=600.0)
try:
    a = COMPOSE_OUTLINE_USER.format(meta=meta, video_duration="00:10:00", duration_min=40,
                                    knowledge="### kp1｜知识点", transcript="[00:00] 你好")
    b = COMPOSE_EXAMPLES_USER.format(meta=meta, knowledge="### kp1｜知识点", transcript="[00:00] 你好")
    check("COMPOSE_OUTLINE_USER 可格式化", "knowledge" in a and "请输出如下 JSON" in a)
    check("COMPOSE_EXAMPLES_USER 可格式化", "请只输出下面这个结构" in b)
except KeyError as exc:
    check("提示词可格式化", False, f"缺占位符 {exc}")

from app.stages.compose import _transcript_slice  # noqa: E402

cues = [Cue(start=float(i * 10), end=float(i * 10 + 8), text=f"第{i}句") for i in range(60)]
wide = _transcript_slice(cues, [KnowledgePoint(id="k", title="t", ts_start=0.0, ts_end=450.0)])
check("范围够宽时只取本批", 40 < wide.count("第") < 60, f"{wide.count('第')} 句 / 共 60")
narrow = _transcript_slice(cues, [KnowledgePoint(id="k", title="t", ts_start=0.0, ts_end=30.0)])
check("范围太窄时回退给全文（免得漏掉上下文）", narrow.count("第") == 60, f"{narrow.count('第')} 句")

print("\n=== 空壳检测：compose 不能交一份空教案出去 ===")
from app.stages.compose import _degenerate  # noqa: E402


def full_lesson() -> Lesson:
    L = Lesson()
    L.objectives = ["目标一", "目标二", "目标三"]
    L.flow = [FlowStep(stage="导入", duration_min=5.0)]
    kp = KnowledgePoint(id="kp1", title="知识点一", ts_start=0.0, ts_end=600.0,
                        summary="讲图像平移", definition="左移即 x+a", formula_latex="y=f(x+a)")
    kp.examples.append(Example(id="ex1", ts_start=10.0,
                               statement_latex="左移 2 个单位", answer="y=(x+2)^2",
                               solution=["第一步"]))
    L.knowledge = [kp]
    return L


check("正常的教案不算空壳", _degenerate(full_lesson()) == "")

# 这一版就是真实翻车的那次：模型"成功"返回，但字段全是空串
hollow = Lesson()
hollow.knowledge = [KnowledgePoint(id=f"kp{i}", title=f"知识点{i}", ts_start=0.0, ts_end=100.0)
                    for i in range(1, 12)]
bad = _degenerate(hollow)
check("空壳教案被认出来", bool(bad), bad[:70])
check("点出了知识点全是空壳", "摘要/定义" in bad)
check("附带说明了目标/流程也空", "教学目标为空" in bad and "课堂流程为空" in bad)

hollow2 = Lesson()
hollow2.objectives = ["目标"]
hollow2.flow = [FlowStep(stage="导入", duration_min=5.0)]
kp2 = KnowledgePoint(id="kp1", summary="讲了", ts_start=0.0, ts_end=100.0)
kp2.examples.append(Example(id="ex1", statement_latex="题干", answer="", solution=[]))
hollow2.knowledge = [kp2]
check("例题全没答案也算空壳", "没有答案或解析" in _degenerate(hollow2), _degenerate(hollow2))

solo = Lesson()
solo.objectives = ["目标"]
solo.flow = [FlowStep(stage="导入", duration_min=5.0)]
solo.knowledge = [KnowledgePoint(id="kp1", summary="讲了", ts_start=0.0, ts_end=100.0)]
check("本来就没例题时不算空壳", _degenerate(solo) == "", _degenerate(solo))

# 少写目标/流程不算致命——那是规则体检的事，一票否决只会误伤好教案
partial = Lesson()
partial.knowledge = [KnowledgePoint(id="kp1", summary="讲了", definition="定义",
                                    ts_start=0.0, ts_end=100.0)]
check("只有内容、没目标流程时不算空壳", _degenerate(partial) == "", _degenerate(partial))

print("\n" + "=" * 50)
print(f"通过 {ok}/{total}")
print("✅ compose 分批逻辑正常" if ok == total else "❌ 分批逻辑有问题")
sys.exit(0 if ok == total else 1)
