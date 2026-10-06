# VCD 波形窗口服务

把仿真器生成的 VCD（Value Change Dump）裁剪为可逐段比较的稳定信号窗口，
消除声明层级、时间单位及同刻赋值顺序差异对回归比对的影响。

## 接口

### `POST /api/vcd/window`（`multipart/form-data`）

| 字段 | 说明 |
| --- | --- |
| `file` | ASCII 编码的 VCD 文件，不超过 **4 MiB** |
| `window` | JSON：`{"start": <fs>, "end": <fs>}`，**半开**区间 `[start, end)` |
| `signals` | 待选信号的**完整层级名**（如 `top.u_cpu.alu_q`）；可重复使用多个表单字段，或提交一个 JSON 数组 |

约束与规则：

- 单次最多处理 **50 000** 次值变更
- 仅接受 1 位 `wire`、规范 `$scope` 作用域、非递减时间戳
- 时标仅接受 `1/10/100` × `fs/ps/ns`（如 `$timescale 1ns $end`，连写或分开均可）
- 信号名/作用域名使用规范标识符；拒绝重复信号名、未声明标识符
- 所有时间统一换算为飞秒；越界换算（超过 63 位有符号整数）被拒绝
- 每个选中信号从**窗口起点处的有效值**开始（未初始化的线网为 `x`）
- 同一时刻的多次赋值按**文本顺序**裁决（取最后一次），不产生零长度区间
- 返回连续覆盖窗口的 `0/1/x/z` 半开区间，相邻同值区间自动合并
- 窗口必须能被完整解释：VCD 中存在不晚于 `start`、不早于 `end` 的时间戳
- 任何错误都返回带原因（和 `line` 定位，如适用）的错误响应，**绝不产生部分结果**

成功响应示例：

```json
{
  "window": {"start": 0, "end": 20000000, "unit": "fs"},
  "signals": {
    "top.clk": [
      {"start": 0, "end": 10000000, "value": "0"},
      {"start": 10000000, "end": 20000000, "value": "1"}
    ]
  }
}
```

错误响应：

```json
{"error": {"code": "TIME_REGRESSION", "message": "...", "line": 9, "details": {}}}
```

### `GET /healthz`

返回 `200 {"status":"ok"}`，供容器/编排层做就绪探测。

## 运行

宿主机端口可配置（默认 8080）：

```bash
VCD_PORT=9090 docker compose up --build web
# 健康检查就绪后执行一次性验证（单元测试 + 构建检查 + HTTP 冒烟），退出码即结果
docker compose up --build verify
```

冒烟覆盖：健康检查、同刻变更的文本顺序裁决、`1ns`/`1ps` 跨时标窗口一致性、
`0/1/x/z` 连续覆盖、时间倒退与窗口越界拒绝（无部分结果）。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/gunicorn --bind 0.0.0.0:8080 app.server:app
```
