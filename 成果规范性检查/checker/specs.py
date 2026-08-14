from __future__ import annotations

from .models import FieldSpec


def F(
    name: str,
    code: str,
    kind: str,
    length: int,
    decimals: int | None = None,
    required: bool = False,
) -> FieldSpec:
    return FieldSpec(name, code, kind, length, decimals, required)


TABLE_5_1 = (
    F("县名称", "QXMC", "Char", 30, required=True),
    F("县代码", "QXDM", "Char", 12, required=True),
    F("乡镇名称", "XZMC", "Char", 30),
    F("乡镇代码", "XZDM", "Char", 9),
    F("村名称", "CUNMC", "Char", 100),
    F("村代码", "CUNDM", "Char", 12),
    F("图斑一级类名称", "TBLXMC", "Char", 16, required=True),
    F("图斑一级类代码", "TBLXDM", "Char", 4, required=True),
    F("图斑二级类名称", "TBEJLMC", "Char", 16),
    F("图斑二级类代码", "TBEJLDM", "Char", 4),
    F("图斑面积", "TBMJ", "Double", 15, 0, True),
    F("影像时间", "YXSJ", "Char", 20, required=True),
    F("备注", "BZHU", "Char", 20),
)

TABLE_5_3 = (
    F("县名称", "QXMC", "Char", 30, required=True),
    F("县代码", "QXDM", "Char", 12, required=True),
    F("测量图斑一级类名称", "CLTBLXMC", "Char", 16, required=True),
    F("测量图斑一级类代码", "CLTBLXDM", "Char", 4, required=True),
    F("测量图斑二级类名称", "CLTBEJLMC", "Char", 16),
    F("测量图斑二级类代码", "CLTBEJLDM", "Char", 4),
    F("测量图斑面积", "CLTBMJ", "Double", 15, 0),
    F("参考图斑一级类名称", "CKTBLXMC", "Char", 16, required=True),
    F("参考图斑一级类代码", "CKTBLXDM", "Char", 4, required=True),
    F("参考图斑二级类名称", "CKTBEJLMC", "Char", 16),
    F("参考图斑二级类代码", "CKTBEJLDM", "Char", 4),
    F("参考图斑面积", "CKTBMJ", "Double", 15, 0, True),
    F("备注", "BZHU", "Char", 20),
)

TABLE_6_1 = (
    F("县名称", "QXMC", "Char", 30, required=True),
    F("县代码", "QXDM", "Char", 12, required=True),
    F("乡镇名称", "XZMC", "Char", 30),
    F("乡镇代码", "XZDM", "Char", 9),
    F("村名称", "CUNMC", "Char", 100),
    F("村代码", "CUNDM", "Char", 12),
    F("普查区代码", "PCQDM", "Char", 12, required=True),
    F("普查区名称", "PCQMC", "Char", 30, required=True),
    F("单位或户码", "DWHHM", "Char", 6, required=True),
    F("图斑一级类名称", "TBLXMC", "Char", 16),
    F("图斑一级类代码", "TBLXDM", "Char", 4),
    F("图斑二级类名称", "TBEJLMC", "Char", 16),
    F("图斑二级类代码", "TBEJLDM", "Char", 4),
    F("养殖场（户）名称", "YZCHMC", "Char", 100, required=True),
    F("养殖场（户）编码", "YZCHBM", "Char", 17, required=True),
    F("分厂编码", "FCBM", "Char", 4),
    F("养殖畜种", "YZXZ", "Char", 10, required=True),
    F("养殖用房编号", "YZYFBH", "Char", 4, required=True),
    F("养殖层数", "YZCS", "Char", 10),
    F("养殖场养殖用房总面积", "YZYFMJ", "Double", 15, 0, True),
    F("图斑面积", "TBMJ", "Double", 15, 0, True),
    F("影像时间", "YXSJ", "Char", 20, required=True),
    F("备注", "BZHU", "Char", 20),
    F("养殖畜种代码", "YZXZDM", "Char", 2),
)

EVALUATION_FIXED = (
    F("县名称", "QXMC", "Char", 30, required=True),
    F("县代码", "QXDM", "Char", 6, required=True),
    F("精度评价人", "PJR", "Char", 30, required=True),
    F("精度评价日期", "PJRQ", "Char", 30, required=True),
)

SCHEMAS = {
    "5-1": TABLE_5_1,
    "5-3": TABLE_5_3,
    "6-1": TABLE_6_1,
    "5-4": EVALUATION_FIXED,
    "6-3": EVALUATION_FIXED,
}
