# 设计说明

## 0. 要回答的问题

Crosier (2026) 用 Kalshi 的温度合约证明：**一群人聚合出来的预报，比官方最好的单产品
（NBM）更准**，7 个城市里 6 个胜出，1 天时效上 RMSE 低约 10%；即使对上「全产品最优加权组合」，
也还有 3–4% 的优势。他用的方法很干净——把市场隐含分布取均值当作一个预报，
让它和一堆官方产品在同一个真值、同一套指标下比赛。

我们要回答的是同一个问题在海洋上的版本：

> 在南海，一群人提交的海温预测，能不能打赢 HYCOM / GFS / CFSv2？
> 如果能，赢多少？在哪些海区、哪些时效赢？

和原文的三处必要差异：

1. **没有交易所**。Kalshi 有真实报价可以读出概率分布，我们没有。所以改成直接提交数值，
   用「同一条信息截止规则」保证和官方产品的信息集可比。
2. **目标量是区域平均**，不是单个气象站。海温没有「城市站点」这种天然聚合点，
   所以定义 7 个固定海区盒子，取区域内 cos(纬度) 加权平均。这消掉了单点噪声，
   也避免玩家挑一个异常格点碰运气。
3. **真值本身有不确定性**。地面气温是实测，SST 的「真值」是卫星反演 + 现场观测的
   最优插值分析场（OISST），它自己也有 0.1–0.4°C 量级的误差。这一层不确定性
   无法消除，但要写在这里，避免把 0.1°C 的差距当成真实差异。

## 1. 真值：NOAA OISST v2.1

- 空间分辨率 0.25°，时间分辨率日平均，1981 年至今
- 来源：NOAA NCEI，经 CoastWatch ERDDAP 提供 CSV 直取（免注册）
- 两套产品：`ncdcOisst21NrtAgg`（近实时，滞后 1–2 天）与 `ncdcOisst21Agg`
  （最终版，滞后 2–3 周，质量更高）。**规则：日期超过 21 天的用最终版并覆盖 NRT 值**，
  所以早期成绩会随最终版发布而被修正一次，这是好事，要保留。
- 海区值 = 区域内所有有效海格点的 cos(纬度) 加权平均，保留两位小数
- 陆地格点在 OISST 里是 NaN，天然被排除；`truth.valid_frac` 记录了海域占比

选择理由：OISST 是全球海温检验里最通用的基准（Reynolds et al. 2007；Banzon et al. 2016），
任何第三方都能下载复现同一套结算结果，不存在「裁判自己说了算」的问题。

**已知问题**：OISST 与其它 SST 分析产品（OSTIA、MUR、HYCOM 的分析场）之间存在
0.1–0.4°C 的系统性差异（Huang et al. 2013）。在我们这里，这个差异会以
「HYCOM 系统性偏冷约 0.3°C」的形式出现在榜单上。这不是 HYCOM 的错，而是两个
产品的基准不同。所以榜单同时给出**偏差**和**相对官方最好产品**两个指标，
让读者能区分「系统性偏差」和「随机误差」。

## 2. 海区定义

7 个矩形海区，覆盖南海主要水文分区，边界尽量避开复杂岸线以减少海陆混淆：

| 代码 | 名称 | 经度 | 纬度 | 特点 |
|---|---|---|---|---|
| `beibu` | 北部湾 | 105–110°E | 17–21.5°N | 半封闭浅水湾，受沿岸径流与季风影响强 |
| `hainan_se` | 海南岛东南 | 108.5–112°E | 16.5–20°N | 冬季受暖流与上升流共同影响 |
| `pearl_river` | 珠江口—东沙 | 112–118°E | 19.5–23°N | 冲淡水与外海交换区，陆架坡折 |
| `xisha` | 西沙 | 109.5–113.5°E | 14.5–17.5°N | 深水为主 |
| `zhongsha` | 中沙—黄岩岛 | 113.5–118.5°E | 13–17°N | 深水 |
| `nansha_n` | 南沙北部 | 110–116°E | 8–13°N | 接近常年暖池边缘 |
| `scs_south` | 南海南部 | 105–115°E | 3–8°N | 赤道附近，季节内振荡（MJO/BSISO）信号强 |

## 3. 截止时间规则

对目标日 D 和时效 h（1/3/5 天），**截止时间 = (D − h) 的 00:00 UTC**。

这个规则的作用是：官方产品和玩家遵守**完全相同的信息截止线**。一个产品的起报时间
必须 ≤ 截止时间才能被计入（见 `tools/ingest_products.py` 里的 `prefer_run`）。
因此，当时效是 5 天时，HYCOM 只能用 5 天前那次起报——哪怕上游两天前又出了新的起报，
也不许用。这样比较才公平，也才和 Crosier 的「同一时点比较」一致。

榜单上会显示每个产品**实际用到的起报日期**，方便核查。

## 4. 评分

### 4.1 基础指标

对每条已结算记录，误差 `e = 预报 − 真值`：

- **MAE** = |e| 的平均
- **RMSE** = √(e² 的平均)，对大误差更敏感
- **Bias** = e 的平均，反映系统性偏高/偏低
- **命中率 Hit** = P(|e| ≤ 0.5°C)

### 4.2 基准（"标准"是谁）

> **基准 MAE = 同一 (海区, 时效) 分组里，表现最好的官方预报产品的 MAE。**

不是我们拍的数字，而是当前官方能给出的最好水平。分组内若还没有任何官方产品数据
（冷启动阶段），退回该组所有条目的中位误差。

这一条是整个评分的核心，它让分数有明确含义：**100 分不封顶于"完美"，
而是锚定在"官方最强"上**。

### 4.3 综合分

```
adv       = 1 − MAE / 基准MAE          # 正数 = 比官方最好还准
AccScore  = clip(0.5 + adv, 0, 1)      # 与官方最好打平 = 0.50
BiasScore = exp(−|Bias| / 0.5)         # 0.5°C 为衰减尺度
HitScore  = P(|e| ≤ 0.5°C)

综合分 = 100 × (0.45×AccScore + 0.20×BiasScore + 0.35×HitScore)
```

**先在每个 (海区, 时效) 分组内各算一遍，再对所有分组取平均。** 这样「好猜的海区」
或「短时效」不会把总分抬起来。样本数少于 10 条不给综合分，只显示原始指标。

设计取舍：

- **为什么把 Bias 单独拎出来**（占 20%）：SST 分析产品之间有 0.2–0.4°C 的基准差异，
  一个只会「永远比别人高 0.5 度」的提交在 RMSE 上可能不差，但它是没信息量的。
  Bias 项把这个抓出来。
- **为什么 AccScore 用「打平 = 0.5」而不是「打平 = 0」**：让榜单在冷启动时
  所有条目都在 50 分上下，可读性更好；同时保留「比官方最好更准」的上升空间。
- **为什么权重是 0.45/0.20/0.35**：准确度是主项，命中率是玩家最直观的感受，
  偏差是防作弊项。全部写在 `data/config.json` 里，可以一键改，界面会显示当前权重。

### 4.4 显著性

`scoring.dm_test()` 实现了 Diebold–Mariano 检验（Diebold & Mariano 1995），
用 Newey–West 标准误修正自相关，和论文一致。用于回答「A 比 B 准」这个差异
是不是统计上站得住。

### 4.5 内置对照：持续性基线

不放进"官方产品"名单，但一定上榜单：**把目标日往前 h 天的实测值直接当预报**。
这是海温预报里最难打败的简单基准，也是判断其它产品有没有价值的第一道关。

## 5. 取数实现要点

### 5.1 一个必须避开的坑：FMRC "best" 聚合

HYCOM 在 THREDDS 上有两种取法：

- `FMRC_ESPC-D-V02_ice_best.ncd`（"best time series" 聚合）
- `FMRC_ESPC-D-V02_ice/runs/...RUN_<起报时刻>`（单次起报）

**best 聚合对每个有效时刻只保留最新的一次起报。** 用它取"5 天时效"的预报，
拿到的其实是"1 天时效"的值——等于给官方产品开小灶。
本项目第一版就踩了这个坑：h=1/3/5 拿到完全相同的数值，HYCOM 的 MAE 被压到 0.24。

改成直接读单次起报数据集后，三个时效才能拿到三次不同的起报，数字才可信。
（修复后 HYCOM MAE：1 天 0.34、3 天 0.32、5 天 0.32。）

### 5.2 海陆掩膜

GFS 和 CFSv2 在陆地上也有数值（不是 SST），直接用会把中南半岛、婆罗洲的
陆地气温混进海域平均。做法：从 OISST 盒子数据里提取"是海"的 0.25° 格点集合，
对产品网格点做邻近查找（容差 0.2°），把陆地剔掉。

### 5.3 单位

- GFS `TMP@surface` 是开尔文，减 273.15
- CFSv2 `ocnsst` 是开尔文，减 273.15
- HYCOM `sst` 已经是 °C

### 5.4 写入并发

抓取任务每天两次、可能持续十几分钟。SQLite 用 WAL 模式，抓取**每处理完一个
(产品, 时效) 就立刻提交**，不能攥着写锁不放——否则玩家提交会被卡住。
服务端启动时的建表也被设计成"锁住就跳过"，不阻塞服务提供只读接口。

## 6. 已知局限（写在这里，避免过度解读）

1. **真值不是绝对真理**。OISST 是分析场，自身误差 0.1–0.4°C。当各条目的
   差距小于这个量级时，排名不应被当作定论。
2. **没有概率型提交**。原文的市场价格天然是概率分布，我们目前只收点值，
   丢失了不确定性信息。后续可以改成提交分位数，用连续排序概率评分（CRPS）。
3. **官方产品只有 3 个，且都不是本区域最优**。真正的最优组合应该包含
   Copernicus CMEMS 全球分析与预报、JMA/CMA 的业务化产品，以及论文里的
   「全产品最优加权组合」。这些都是 roadmap。
4. **回补深度受上游保留期限制**。NOMADS 只保留约 10 天 GFS 起报，
   HYCOM 的 FMRC 只保留 8 次起报。冷启动阶段样本少，要跑一段时间才有统计意义。
5. **HYCOM 上游起报节奏约滞后 2 天**，所以它在短时效上也拿不到最新起报，
   这在规则上是公平的（大家都受同一截止线约束），但会让它显得吃亏。

## 7. 参考文献

方法学与真值：

1. Crosier, A. W. (2026). *Prediction Markets Beat the Weather Forecast on Tomorrow's
   High Temperature.* arXiv:2609.23969v1 [q-fin.GN]. — 本项目的直接原型。
2. Reynolds, R. W., et al. (2007). Daily High-Resolution-Blended Analyses for Sea
   Surface Temperature. *Journal of Climate*, 20(22), 5473–5496. doi:10.1175/2007JCLI1824.1
3. Banzon, V., et al. (2016). A 1/4°-Spatial-Resolution Daily Sea Surface Temperature
   Climatology Based on a Blended Satellite and in situ Analysis. *Journal of Climate*,
   29(24), 8933–8946. doi:10.1175/JCLI-D-14-00293.1
4. Huang, B., et al. (2013). Why Did Large Differences Arise in the Sea Surface
   Temperature Datasets across the Tropical Pacific? *JAOT*, 30(12), 2940–2953.
   doi:10.1175/JTECH-D-13-00034.1
5. Diebold, F. X., & Mariano, R. S. (1995). Comparing Predictive Accuracy.
   *Journal of Business & Economic Statistics*, 13(3), 253–263.
   doi:10.1080/07350015.1995.10524599
6. Murphy, A. H. (1973). A New Vector Partition of the Probability Score.
   *Journal of Applied Meteorology*, 12(4), 595–600. — 技巧分（skill score）的经典来源。
7. Gneiting, T., & Raftery, A. E. (2007). Strictly Proper Scoring Rules, Prediction,
   and Estimation. *JASA*, 102(477), 359–378. doi:10.1198/016214506000001437

官方预报产品：

8. Barton, N. P., et al. (2020). The Navy's Earth System Prediction Capability:
   A New Global Coupled Atmosphere–Ocean–Sea Ice Prediction System. *Earth and Space
   Science*, 7, e2019EA001199. doi:10.1029/2020EA001199 — HYCOM ESPC-D-V02 的系统说明。
9. Saha, S., et al. (2014). The NCEP Climate Forecast System Version 2.
   *Journal of Climate*, 27(6), 2185–2208. doi:10.1175/JCLI-D-12-00823.1
10. Bleck, R. (2002). An oceanic general circulation model framed in hybrid
    isopycnic–Cartesian coordinates. *Ocean Modelling*, 4(1), 55–88. — HYCOM 内核。
11. Chassignet, E. P., et al. (2007). The HYCOM data assimilative ocean prediction
    system. *Journal of Marine Systems*, 65, 60–83.

南海海温与可预报性：

12. Chu, P. C., et al. (1997). Temporal and spatial variabilities of the South China
    Sea surface temperature anomaly. *JGR Oceans*, 102(C10). doi:10.1029/97JC00982
13. Tan, H., et al. (2022). Summer marine heatwaves in the South China Sea: Trend,
    variability and possible causes. *Advances in Climate Change Research*.
    doi:10.1016/j.accre.2022.04.003
14. Latif, M., et al. (1998). A review of the predictability and prediction of ENSO.
    *JGR Oceans*, 103(C7). doi:10.1029/97JC03413
15. Penland, C., & Sardeshmukh, P. D. (1995). Error and Skill in One-Tier and Two-Tier
    Forecasts of Tropical Pacific SST. *Journal of Climate*. — 持续性 vs 动力预报的经典比较。

深度学习 SST 预报（对接 roadmap 里的"玩家模型"）：

16. Xiao, C., et al. (2019). A spatiotemporal deep learning model for sea surface
    temperature field prediction. *Environmental Modelling & Software*, 120, 104502.
    doi:10.1016/j.envsoft.2019.104502
17. Ham, Y.-G., Kim, J.-H., & Luo, J.-J. (2019). Deep learning for multi-year ENSO
    forecasts. *Nature*, 573, 568–572. doi:10.1038/s41586-019-1559-7
18. Kochkov, D., et al. (2024). Neural general circulation models for weather and
    climate. *Nature*, 632, 1060–1066. doi:10.1038/s41586-024-07744-y

预测市场与信息聚合（为什么"一群人"可能更准）：

19. Hayek, F. A. (1945). The Use of Knowledge in Society. *American Economic Review*,
    35(4), 519–530.
20. Berg, J. E., et al. (2003). Accuracy and Forecast Standard Error of Prediction
    Markets. — 预测市场准确度的早期系统性证据。
21. Gürkaynak, R. S., & Wolfers, J. (2007). Macroeconomic Derivatives.
    In *NBER International Seminar on Macroeconomics 2005*. doi:10.7551/mitpress/7523.003.0004
22. Cowgill, B., & Zitzewitz, E. (2015). Corporate Prediction Markets: Evidence from
    Google, Ford, and Firm X. *Review of Economic Studies*, 82(4), 1309–1341.
    doi:10.1093/restud/rdv014
