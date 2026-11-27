"""Localize authored labels in the offline report and geometry viewer.

Templates are expanded before inserting model evidence or file names.
"""

from __future__ import annotations

import html
import json
import re


_EN = {
    "no_current_height_binding": "No height evidence bound to this opening",
    "source_view_or_value_changed": "Source view or value changed",
    "no_elevation_calibration": "Elevation calibration missing",
    "whole_image_only": "Only the full image was referenced",
    "outside_source_region": "Outside the source region",
    "needs_per_opening_confirmation": "Individual opening confirmation needed",
    "distinct_width_on_facade": "Atypical width on this façade",
    "shared_height_across_widths": "Openings with different widths share a height",
    "residual_exceeds_threshold": "Calibration residual exceeds its limit",
    "X、Y 轴标定分别为 {x_px_per_m:.9g} 和 {y_px_per_m:.9g} px/m，相差 {relative_deviation:.2%}，超过 {relative_deviation_limit:.2%}。请核对两轴的尺寸界线端点和标注值，再重新标定。":
        "X and Y scales are {x_px_per_m:.9g} and {y_px_per_m:.9g} px/m. Their difference of {relative_deviation:.2%} "
        "exceeds the {relative_deviation_limit:.2%} limit. Recheck the extension-line endpoints and dimensions on both axes, then recalibrate.",
    "源 BIM 候选：几何检查通过，原图保真待评价": "Source BIM candidate · geometry checks passed, drawing fidelity unverified",
    "源 BIM 候选：几何检查有严重问题，仍保留查看结果": "Source BIM candidate · severe geometry issues",
    "模型来源与说明": "Model source and notes",
    "模型提交的几何方案；原图保真尚未核对，未经人工确认。": "The model submitted this geometry. Drawing fidelity and human review are pending.",
    "未解决项": "Outstanding items",
    "质量与来源记录": "Quality and source records",
    "原始方案": "Original proposal",
    "源模型": "Source model",
    "楼层": "Floor",
    "墙面": " wall",
    "· 楼面标高": "· Floor elevation",
    "南北向墙线": "North–south wall line",
    "东西向墙线": "East–west wall line",
    "平面对象": "Plan element",
    "· 删除": "· Removed",
    "个窄条空间标记": "narrow-space markers",
    "· 合并": "· Merged",
    "个重复门窗": "duplicate openings",
    "· 跨层外轮廓边对齐": "· Aligned footprint edges across floors",
    "规整规则": "Geometry adjustment rule",
    "原因": "Reason",
    "未找到可靠对应": "No reliable match",
    "内墙": "Partition",
    "外轮廓": "Footprint",
    "门窗": "Opening",
    "尺寸基准": "Dimension reference",
    "墨线": "Drawing line",
    "尺寸标注": "Dimension annotation",
    "见完整记录": "See full record",
    "核对": "Checked",
    "米": "m",
    "；偏差": " · Deviation",
    "厘米": "cm",
    "；自动对齐未安全完成，保留原值并随交付说明。": ". Automatic alignment could not be completed. The original value was retained.",
    "几何规整清单": "Geometry adjustments",
    "以下记录工具对原读数的调整；读图是否准确仍需对照原图。": "The tool adjusted the readings listed below. Compare them with the original drawings to verify their accuracy.",
    "对象": "Element",
    "从 → 到（米）": "From → To (m)",
    "移动（厘米）": "Displacement (cm)",
    "依据": "Basis",
    "本稿没有几何移动。": "No elements were moved.",
    "读图坐标调整": "Drawing coordinate adjustments",
    "采用方式": "Method",
    "未采用或需返工：": "Rejected or requiring revision: ",
    "未对齐并保留原值": "Alignment skipped; original values retained",
    "未采用的读数调整，保持原值：": "Rejected adjustments, with original values retained: ",
    "完整规整记录与内部编号": "Full adjustment records and identifiers",
    "未回查": "Not reviewed",
    "仅有局部回查": "Partially reviewed",
    "与所报观察一致": "Consistent with reported observations",
    "仍需跟进": "Follow-up needed",
    "门洞": "Doorway",
    "窗": "Window",
    "空通道": "Passage",
    "北": "North",
    "南": "South",
    "东": "East",
    "西": "West",
    "逐立面回查范围": "Review coverage by façade",
    "零个已建开口也需明确观察；内部及无法确定方向的开口不能靠立面回查覆盖。": "A count of zero still requires an observation. Interior openings and those of unknown orientation are outside the façade review. ",
    "与所报观察一致仍不代表原图保真。": "Agreement with observations does not establish drawing fidelity.",
    "立面": "Façade",
    "类别": "Type",
    "已建数量": "Modeled count",
    "回查状态": "Review status",
    "尚未清点": "Not counted",
    "清点记录冲突": "Conflicting counts",
    "与所报数量一致": "Matches reported count",
    "数量不符": "Count mismatch",
    "逐层逐面外墙门窗清点": "Exterior opening counts by floor and façade",
    "数量来自所报原图观察；相等不证明位置正确。": "Counts come from observations of the original drawings. Locations require a separate check. ",
    "未清点、数量不符和冲突均只报告，不阻止交付。": "Uncounted openings, mismatches and conflicts are recorded here.",
    "已建": "Modeled",
    "所报原图数量": "Reported drawing count",
    "结果": "Result",
    "清点报告暂不可用：": "Count report unavailable: ",
    "部分观察无法绑定当前楼层/立面或原图已变化，或外墙方向未确定；详见 delivery.json 的 facade_counts。": "Some observations could not be matched to this floor, façade or drawing revision, or wall orientation is unknown. See facade_counts in delivery.json.",
    "有定位依据，已应用": "Located and applied",
    "有定位依据，已确认": "Located and confirmed",
    "假设／声明": "Assumed or declared",
    "缺少逐扇定位依据": "Individual location evidence missing",
    "逐开口高度表": "Opening heights",
    "一行对应一扇外部开口。状态说明数值应用和来源定位，": "Each row represents an exterior opening. The status records whether its value was applied and its source located. ",
    "不证明读图正确；同一定位框包含多扇时需逐扇确认。假设、缺项与待核原因不阻止交付。": "Check each opening against the drawing, including openings that share a reference box. Assumptions, missing values and follow-up items are listed here.",
    "开口": "Opening",
    "绝对高度（米）": "Absolute elevation (m)",
    "状态": "Status",
    "依据与视图": "Evidence and views",
    "待核原因": "Follow-up reason",
    "高度表暂不可用：": "Height table unavailable: ",
    "已返回整图": "Full image returned",
    "仅返回局部": "Crop returned",
    "无直接看图记录": "No direct view recorded",
    "本次原图直接查看记录": "Original drawing views in this run",
    "仅统计本次直接看图工具的返回；": "These records cover images returned by the direct-view tool in this run. ",
    "其他图像工具、局部模型和前次运行不计入。返回整图不等于已检查全图或读图正确，": "Other image tools, partial models and previous runs are outside their scope. A full image return does not confirm complete inspection, drawing accuracy ",
    "也不等于高度已落实到模型。": "or the application of heights to the model.",
    "原图": "Original drawing",
    "返回范围": "View returned",
    "：": ": ",
    "继承观察（尚未复核）：": "Inherited observation awaiting review: ",
    "已应用，当前仍保留": "Applied and retained",
    "已核对，无需修改": "Checked and unchanged",
    "已采纳，尚未关联执行或确认": "Accepted, awaiting application or confirmation",
    "仅部分值已落实": "Partially applied",
    "检查后对象已改变，需复核": "Element changed since review",
    "暂缓": "Deferred",
    "已撤回": "Retracted",
    "待判断": "Undecided",
    "；": "; ",
    "已撤销": "Removed",
    "通过": "Passed",
    "有警告": "Warnings",
    "有严重问题": "Severe issues",
    "未评价": "Not evaluated",
    "模型选定": "Selected by the model",
    "系统保留的最新候选，模型未显式选定": "Latest saved candidate (no explicit model selection)",
    "本次模型调用正常结束。": "Model generation completed.",
    "模型调用尚未结束。": "Model generation is in progress.",
    "本次模型调用未正常完成，以下保留已生成候选。": "Generation was interrupted. The saved candidate is shown below.",
    "已选定并保存 BIM，未完成事项见下方。": "The selected BIM has been saved. Outstanding items are listed below.",
    "调度员已声明本稿覆盖整栋建筑，声明已绑定到当前模型和输入资料。": "The coordinator declared whole-building coverage for this model and these inputs.",
    "已核实声明引用的回叠图属于本稿。": "The referenced overlays were verified against this candidate.",
    "本稿尚无有效的整栋范围声明，保留为局部或待核查交付。": "Whole-building coverage is unconfirmed. This is a partial delivery or one awaiting review.",
    "整栋": "whole building",
    "局部": "partial building",
    "调度员声明的交付范围为": "The coordinator declared the following scope: ",
    "。": ". ",
    "尚存疑问：": "Outstanding questions: ",
    "本稿不完整：尚缺楼层图": "Missing floor drawings: ",
    "已覆盖预期楼层图；这不证明房间、门窗或高度正确。": "All expected floor drawings are covered. Room layout, openings and heights require separate verification.",
    "未声明可核对的楼层图范围，全楼完整性未判定。": "Expected floor drawings were not specified. Whole-building coverage is undetermined.",
    "尺寸证据与所引用墙段的位置对照": "Dimension evidence and referenced wall segments",
    "图中 W 为已引用墙段，D 的 S/E 为原记录起点/终点。下表距离表示证据点沿墙方向": "W marks the referenced wall segment. S/E on D mark the recorded endpoints. The distances below measure how far each point extends ",
    "超出该墙段范围的长度，依赖本图标定；0 不证明归属正确，尺寸引出线也可能合理地落在墙段外。": "past its segment along the wall, using this drawing's calibration. A zero distance does not confirm a match. Extension lines may legitimately fall outside a segment.",
    "原图 / 楼层": "Drawing / floor",
    "证据点": "Evidence point",
    "所引用墙段": "Referenced wall segment",
    "沿墙超出范围（米）": "Distance beyond segment (m)",
    "尺寸与标定的实际反馈": "Dimension and calibration checks",
    "以下为工具计算，原始数值及未处理问题不会被模型总结覆盖。": "These measurements and outstanding findings come from the geometry tools. ",
    "尺寸残差不自动等于建模错误；同墙侧面次序冲突应先核对端点。单位：米。": "Check endpoints when wall-face order conflicts. Residuals may have causes other than modeling errors. All values are in meters.",
    "尺寸": "Dimension",
    "原标注": "Drawing value",
    "换算后代表面距": "Converted face distance",
    "模型减换算值": "Model minus converted value",
    "当前源回叠图": "Current source overlay",
    "（当前源无该楼层）": " (floor missing from current source)",
    "（当前源没有任何登记图面）": " (no registered drawing in current source)",
    "原图回叠反馈": "Drawing overlays",
    "标定由模型提供，尚未独立验证；图像已生成不代表已审视或原图保真。": "The model provided the calibration. Independent verification, overlay review and drawing fidelity remain separate checks.",
    "当前源投影": "Current source overlays: ",
    "份；旧源投影": " · Previous source overlays: ",
    "份；登记但当前楼层未覆盖": " · Registered views missing a current floor: ",
    "份；当前源无登记图面楼层": " · Current floors without registered views: ",
    "个。": ".",
    "当前源没有成功的回叠图。": "No overlay was successfully generated for this source.",
    "旧源投影": "Previous source overlays",
    "无": "None",
    "未覆盖或失败": "Missing or failed projections",
    "BIM 候选与实际检查": "BIM candidate and verification report",
    "以下状态来自保存的源模型和回查记录。": "These findings come from the saved source model and review records. ",
    "几何自洽或观察对应不等于原图保真；未核查和待处理问题见下方记录。": "Geometric consistency and matching observations do not establish drawing fidelity. Unverified and outstanding items are recorded below.",
    "个空间，": "spaces, ",
    "个已建开口，": "modeled openings, ",
    "个未建开口。": "unmodeled openings. ",
    "几何自洽：": "Geometric consistency: ",
    "；原图保真：未评价。": ". Drawing fidelity: not evaluated.",
    "打开模型": "Open model",
    "源 BIM": "Source BIM",
    "检查记录": "Verification records",
    "原图观察回查": "Drawing observation review",
    "份旧源回查未用于当前候选。": "previous-source reviews were excluded from this candidate.",
    "尚未解决": "Outstanding items",
    "模型未填写；仍需结合上表判断未核查范围。": "No items were supplied by the model. See the tables above for unreviewed areas.",
    "模型采用的假设": "Model assumptions",
    "当前候选的观察结论": "Observation findings for this candidate",
    "以下核对实际参数和宿主是否仍保留，不代表原图判断已经验证。": "These checks verify that parameters and hosts are retained. The original drawing interpretation requires separate verification.",
    "观察": "Observation",
    "当前状态": "Current status",
    "已替代的说明": "Superseded notes",
    "无显式替代记录。": "No superseded notes were recorded.",
    "观察与实际应用": "Observations and applications",
    "采纳、执行成功与原图正确分别判断；": "Acceptance, successful application and drawing fidelity are assessed separately. ",
    "历史应用成功不代表当前候选保留该结果。完整记录见检查记录。": "A previously applied result may have changed in this candidate. See the verification records for details.",
    "保存的 BIM 候选": "Saved BIM candidate",
    "、": ", ",
    "门": "Door",
    "空开口": "Open passage",
    "名称": "Name",
    "所属面": "Parent surface",
    "所属房间": "Room",
    "所属楼层": "Floor",
    "源父对象 ID": "Source parent ID",
    "内部编号与原始记录": "Identifiers and source records",
    "封闭": "Enclosed",
    "半开敞": "Semi-open",
    "开敞": "Open",
    "实体": "Physical",
    "局部混合": "Mixed",
    "未知": "Unknown",
    "墙厚": "Wall thickness",
    "厚度记录冲突，待确认": "Conflicting thickness records",
    "厚度判定": "Thickness assessment",
    "图纸读数": "Drawing measurement",
    "推断": "Inferred",
    "未注明": "Unspecified",
    "取线位置": "Reference line",
    "墙中心线": "Wall centerline",
    "世界负方向侧墙面": "Wall face on the negative world axis",
    "世界正方向侧墙面": "Wall face on the positive world axis",
    "厚度依据": "Thickness evidence",
    "墙厚来源": "Thickness sources",
    "底标高": "Base elevation",
    "源楼层 ID": "Source floor ID",
    "原楼层名": "Original floor name",
    "明确开敞区域": "Open region",
    "围护未知区域": "Region with unknown enclosure",
    "空间": "Space",
    "面积": "Area",
    "边界覆盖": "Boundary coverage",
    "证据类型": "Evidence type",
    "假设": "Assumptions",
    "源边界 ID": "Source boundary ID",
    "来源": "Sources",
    "连通": "Connects",
    "室外": "Outdoors",
    "开闭状态": "Opening state",
    "开放": "Open",
    "关闭": "Closed",
    "未确定": "Undetermined",
    "源开口 ID": "Source opening ID",
    "显示对象 ID": "Display object ID",
    "功能": "Room use",
    "功能判定": "Use assessment",
    "图文明确": "Documented",
    "待判定": "Undetermined",
    "功能依据": "Room-use evidence",
    "功能假设": "Room-use assumptions",
    "空间开敞性": "Space enclosure",
    "源空间 ID": "Source space ID",
    "显示语义": "Representation",
    "未知围护（未当作实体墙）": "Unknown enclosure",
    "实体围护": "Physical enclosure",
    "边界围护": "Boundary enclosure",
    "当前实体片面积；开敞或未知区域另以半透明灰色辅助面显示": "Area of this physical surface fragment. Open and unknown regions appear as translucent gray guides.",
    "源对象 ID": "Source object ID",
    "贴图加载失败，请重新打开完整 HTML。": "Texture loading failed. Open the complete HTML file again.",
    "展开时暂停贴图叠加": "Texture overlay is paused in exploded view.",
    "粉色：BIM边界 · 绿色：窗 · 橙色：门；遮挡处可剖切查看。": "Pink: BIM edges · Green: windows · Orange: doors. Use section cuts to see occluded elements.",
    "原始贴图按已保存坐标对齐，可随剖切和楼层筛选。": "The texture follows the saved coordinates, section cuts and floor filter.",
    "源高度分组": "Source elevation groups",
    "源建筑模型": "Source building model",
    "源空间": "Source spaces",
    "源边界": "Source boundaries",
    "源开口": "Source openings",
    "源几何检查": "Source geometry check",
    "派生映射检查": "Derived mapping check",
    "已记录的门/开口连接": "Recorded doorway and opening connections",
    "半开敞 / 开敞空间": "Semi-open / open spaces",
    "开敞 / 未知边界": "Open / unknown boundaries",
    "逻辑闭合只界定空间范围，不表示实体密闭或热区。半透明灰色是开敞或未知围护的查看辅助面；点选后可区分语义与来源。": "Logical closure defines the extent of a space. Select a translucent gray region to inspect its enclosure type and source.",
    "楼层 / 高度范围": "Floor / elevation range",
    "识别对照": "Reference mesh",
    "叠加原始 textured mesh": "Overlay source textured mesh",
    "贴图 + BIM轮廓": "Texture + BIM edges",
    "贴图 + BIM实体": "Texture + BIM solids",
    "仅贴图": "Texture only",
    "贴图透明度": "Texture opacity",
    "贴图用于视觉对照；半透明区域不作颜色或表面量测。": "Use the texture for visual comparison. Semi-transparent regions do not support color or surface measurements.",
    "门 / 空开口": "Doors / open passages",
    "逻辑空间边界（细虚线）": "Logical space boundaries (dashed)",
    "开敞 / 未知辅助面": "Open / unknown guides",
    "开敞 / 未知（点选查看类型）": "Open / unknown (select to inspect)",
}

_ZH = {
    "no_current_height_binding": "未关联此开口的高度依据",
    "source_view_or_value_changed": "原图视图或数值已变化",
    "no_elevation_calibration": "缺少立面图标定",
    "whole_image_only": "只引用了整张图",
    "outside_source_region": "位置超出原图引用区域",
    "needs_per_opening_confirmation": "需要逐扇确认",
    "distinct_width_on_facade": "该开口的宽度与同立面其他开口有显著差异",
    "shared_height_across_widths": "不同宽度的开口共用高度",
    "residual_exceeds_threshold": "标定残差超过阈值",
    "The declared sides run opposite to the dimension direction or refer to the same face. Check endpoint side labels and direction against the image; moving the wall cannot fix a same-wall span.":
        "已填写的墙面侧别与标注方向相反，或两个端点指向同一面。请对照原图核对端点侧别和方向，平移墙体无法改变同一面上的跨度。",
    "The dimension and declared face offsets disagree on this one wall. Recheck their identity/units; do not move room geometry to close this span.":
        "该墙的标注尺寸与面的偏移量不一致。请核对标注对象及单位，保留房间几何。",
    "叠加原始 textured mesh": "叠加原始贴图网格",
    "Geometry inspection": "几何检查",
    "select by": "选择对象",
    "zone": "空间",
    "floor": "楼层",
    "surface": "表面",
    "edge": "边线",
    "all floors": "所有楼层",
    "display": "显示",
    "Opacity": "不透明度",
    "exploded view": "分解视图",
    "by floor": "按楼层",
    "by zone": "按空间",
    "wall / floor / roof faces": "墙面、楼板与屋面",
    "windows": "窗",
    "edges": "边线",
    "tools": "工具",
    "measure (vertex &rarr; vertex)": "测量两点距离",
    "clear measure": "清除测量",
    "save PNG": "保存 PNG",
    "reset view": "重置视图",
    "measure: snap vertex 1  (Esc to exit)": "选择第一个顶点（Esc 退出）",
    "measure: snap vertex 2  (Esc to exit)": "选择第二个顶点（Esc 退出）",
    "distance: ": "距离：",
    " m  (click=next, Esc=exit)": " 米（单击继续，Esc 退出）",
    "length": "长度",
    "volume": "体积",
    "type": "类型",
    "area": "面积",
    "note": "说明",
    "MODEL": "模型",
    "BOUNDING BOX": "包围盒",
    "zones": "空间",
    "surfaces": "表面",
    "width (x)": "宽度（X）",
    "depth (y)": "进深（Y）",
    "height (z)": "高度（Z）",
    "floors": "楼层",
    "section cuts": "剖切",
    "cut ": "剖切 ",
    "flip side": "翻转方向",
    "building geometry": "建筑几何",
    "Display triangulation does not cover its boundary": "显示网格未覆盖其边界。",
    "Wall": "墙",
    "Floor": "楼板",
    "Ceiling": "天花",
    "Roof": "屋面",
    "Window": "窗",
    "pass": "通过",
    "warning": "有警告",
    "severe": "有严重问题",
    "unknown": "未知",
    "untyped": "未分类",
    "manual_annotation": "人工标注",
    "example": "示例",
    "inferred": "推断",
    "observed": "已观察",
}


def translator(locale: str):
    if locale not in ("zh-CN", "en"):
        raise ValueError(f"Unsupported display language: {locale!r}")
    labels = _EN if locale == "en" else _ZH
    return lambda message: labels.get(message, message)


def localize_template(template: str, locale: str, *, javascript: bool = False) -> str:
    """Expand only marked, authored text; retain script and HTML escaping."""
    translate = translator(locale)

    def expand(match):
        value = translate(match.group(1))
        if javascript:
            return (json.dumps(value, ensure_ascii=False)[1:-1]
                    .replace("'", r"\u0027").replace("<", r"\u003c"))
        return html.escape(html.unescape(value), quote=True)

    return re.sub(r"\[\[msg:(.*?)\]\]", expand, template)


_VIEWER_LABELS = {
    "Model": "模型", "Confidence": "置信度", "Viewer mode": "查看模式", "Spatial confidence": "空间置信度",
    "Edit": "编辑", "Overall changes": "整体调整",
    "Submit edits…": "提交修改…",
    "Describe changes to the model…": "填写整体调整说明…",
    "Confirm the object note or click empty space to discard it.": "请先点勾确认当前批注，或点空白处取消。",
    "Changes are available in this tab only.": "修改仅保留在当前页面。",
    "Notes are saved in this browser for this model.": "整体说明和已确认的批注保存在当前浏览器中，可再次打开此模型查看。",
    "Cancel": "取消",
    "Unassessed": "未评估", "Assumed": "假设建模", "Indirect support": "间接佐证", "Partial support": "部分佐证",
    "Strong support": "充分佐证", "Direct evidence": "直接证据", "Evidence and reasoning": "查看依据",
    "Material": "材料", "Thickness": "厚度", "Not applicable": "不适用", "Not recorded": "未记录",
    "Modeled volume": "模型体积", "Storey volume": "本层体积", "Face area": "表面积",
    "Length": "长度", "Parent face": "所属面", "Type": "类型", "Space count": "空间数",
    "Elevation": "底标高", "Exterior": "室外",
    "Distance": "距离", "Select the first vertex": "选择第一个顶点",
    "Adjust north": "调整指北针", "North bearing": "北向方位角",
    "Clockwise from model +Y": "从模型 +Y 轴顺时针量取",
    "Confirm north bearing": "确认北向", "Cancel north adjustment": "取消北向调整",
    "Select the second vertex": "选择第二个顶点",
    "View settings": "视图设置", "Elevation groups": "高程分组", "Statistics": "统计", "Dimensions": "尺寸",
    "Spaces": "空间", "Boundaries": "边界", "Openings": "开口", "Connections": "连接",
    "Layers": "图层", "Room types": "房间类型", "Width": "宽度", "Depth": "进深", "Height": "高度",
    "Language": "语言", "Selection": "选择对象", "Measure": "测量", "Export PNG": "导出 PNG",
    "Floor separation": "楼层分解", "Comparison": "对照", "Controls": "视图控制", "Building elements": "建筑构件",
    "Name": "名称", "Storey": "楼层", "Function": "功能", "Room": "房间", "Boundary": "边界",
    "Exterior wall": "外墙", "Interior wall": "内墙", "Floor slab": "楼板", "Edge": "边线",
    "Door": "门", "Passage": "通道", "Open boundary": "开敞边界", "Unknown enclosure": "围护未确定",
    "Wall": "墙", "Window": "窗", "Roof": "屋面", "Ceiling": "天花",
}
_MESSAGE = re.compile(r"\[\[msg:(.*?)\]\]")


_ROOM_ALIASES = {
    "办公室": "office", "辦公室": "office", "办公空间": "office", "独立办公": "office/enclosed",
    "會議室": "conference/meeting/multipurpose", "会议厅": "conference/meeting/multipurpose",
    "休息室": "lounge/breakroom", "茶歇室": "lounge/breakroom", "茶水间": "lounge/breakroom",
    "走道": "corridor", "通廊": "corridor", "樓梯間": "stairwell", "楼梯": "stairwell",
    "机电设备房": "electrical/mechanical", "设备房": "electrical/mechanical", "機房": "electrical/mechanical",
    "设备间": "electrical/mechanical", "电梯井": "shaft", "管井": "shaft", "電梯井": "shaft",
    "商铺": "retail", "商舖": "retail", "店铺": "retail", "大堂": "lobby", "门厅": "lobby",
    "廁所": "restroom", "洗手间": "restroom", "洗手間": "restroom", "厕所": "restroom",
}


def room_role(value):
    """Resolve imported labels and the pinned source BIM taxonomy."""
    from src.agent.roles import ROOM_TYPES, normalize

    role = _ROOM_ALIASES.get(str(value or "").strip()) or normalize(value)
    return role if role in ROOM_TYPES else "unknown"


def room_type_labels(room_types):
    """Retain canonical codes and colors, with a readable English display name."""
    familiar = {
        "office/enclosed": "Enclosed office", "conference/meeting/multipurpose": "Meeting / multipurpose room",
        "electrical/mechanical": "Electrical / mechanical room", "lounge/breakroom": "Lounge / break room",
        "retail": "Retail space", "unknown": "Unclassified",
    }
    return {code: {**record, "label_en": familiar.get(code, code.replace("/", " / ").capitalize())}
            for code, record in room_types.items()}


def viewer_messages(*templates):
    """Include only UI strings used by the standalone viewer, in both languages."""
    keys = set(_VIEWER_LABELS)
    for template in templates:
        keys.update(_MESSAGE.findall(template))
        keys.update(re.findall(r"""data-i18n=["']([^"']+)["']""", template))
    en, zh = translator("en"), translator("zh-CN")
    result = {}
    for key in sorted(keys):
        label = html.unescape(en(key))
        result[key] = [label[:1].upper() + label[1:], _VIEWER_LABELS.get(key, zh(key))]
    return result


def viewer_markup(template, messages):
    """Render English immediately and mark only authored labels for live translation."""
    def attr(value):
        return html.escape(value, quote=True)

    def local_attr(match):
        name, key = match.groups()
        return f'{name}="{attr(messages[key][0])}" data-i18n-{name}="{attr(key)}"'

    template = re.sub(r'\b(aria-label|title|placeholder)="\[\[msg:([^\]]+)\]\]"', local_attr, template)
    # Option and title elements contain only text; a span would be invalid.
    template = re.sub(r'(<(?:option|title)\b[^>]*?)>\[\[msg:([^\]]+)\]\](?=</)',
                      lambda m: f'{m[1]} data-i18n="{attr(m[2])}">{attr(messages[m[2]][0])}', template)
    return _MESSAGE.sub(lambda m: f'<span data-i18n="{attr(m[1])}">{attr(messages[m[1]][0])}</span>', template)


def viewer_script(template):
    """Compile authored messages within the JavaScript template to live lookups."""
    def call(key):
        return f"msg({json.dumps(key, ensure_ascii=False)})"
    template = re.sub(r"'\[\[msg:([^\]]*)\]\]'", lambda m: call(m[1]), template)
    return _MESSAGE.sub(lambda m: "'+" + call(m[1]) + "+'", template)
