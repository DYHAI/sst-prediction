"""南海（South China Sea）竞猜海区定义。

每个海区是一个经纬度矩形盒子。结算值 = NOAA OISST v2.1 日平均 SST
在该盒子内所有有效海格点上的 cos(lat) 加权平均。

命名与边界尽量贴近业务上常用的分区，方便和官方预报产品、文献对照。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str
    name_cn: str
    name_en: str
    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name_cn": self.name_cn,
            "name_en": self.name_en,
            "bbox": [self.lon_min, self.lat_min, self.lon_max, self.lat_max],
            "note": self.note,
        }


# 7 个海区，对应论文里 7 个城市的角色。
REGIONS: tuple[Region, ...] = (
    Region("beibu", "北部湾", "Beibu Gulf", 105.0, 110.0, 17.0, 21.5,
           "半封闭浅水湾，受沿岸径流与季风影响强"),
    Region("hainan_se", "海南岛东南", "Hainan SE", 108.5, 112.0, 16.5, 20.0,
           "海南岛东南外海，冬季受暖流与上升流共同影响"),
    Region("pearl_river", "珠江口—东沙", "Pearl River–Dongsha", 112.0, 118.0, 19.5, 23.0,
           "珠江口冲淡水与外海交换区，陆架坡折"),
    Region("xisha", "西沙", "Xisha (Paracel)", 109.5, 113.5, 14.5, 17.5,
           "西沙群岛海域，深水为主"),
    Region("zhongsha", "中沙—黄岩岛", "Zhongsha–Scarborough", 113.5, 118.5, 13.0, 17.0,
           "中沙群岛与黄岩岛一带，深水"),
    Region("nansha_n", "南沙北部", "Nansha North", 110.0, 116.0, 8.0, 13.0,
           "南沙群岛北部，接近常年暖池边缘"),
    Region("scs_south", "南海南部", "SCS South", 105.0, 115.0, 3.0, 8.0,
           "南海南部，赤道附近，季节内振荡信号强"),
)

BY_CODE: dict[str, Region] = {r.code: r for r in REGIONS}

# 竞猜时效（天）
HORIZONS: tuple[int, ...] = (1, 3, 5)


def get(code: str) -> Region:
    return BY_CODE[code]


def all_codes() -> list[str]:
    return [r.code for r in REGIONS]
