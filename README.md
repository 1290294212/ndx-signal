# NDX F-V1.1 云端信号桥（GitHub Actions + GitHub Pages）

这个仓库用于**不依赖个人电脑**，每天从公开渠道读取纳斯达克100日线、计算周度仓位研究信号，并向一个公开的 GitHub Pages JSON 地址发布结果。**不会交易，不包含账户资金、持仓、密钥或个人信息。**

## 一次性部署（约 5 步）

1. 在 GitHub 新建一个**公开仓库**，例如 `ndx-signal`，上传本压缩包**内的全部文件和文件夹**到仓库根目录（保留 `.github/workflows/ndx-daily.yml`）。不要只上传 ZIP 本身。
2. 打开仓库 `Settings → Pages`，在 `Build and deployment` 的 Source 选择 **GitHub Actions**。
3. 到 `Actions → Publish daily NDX strategy signal`，点击 `Run workflow` 手动运行首次任务（GitHub Actions可能需要先在仓库设置中启用）。
4. 部署成功后访问：`https://你的GitHub用户名.github.io/ndx-signal/signal.json`（如果不是该仓库名，按实际名称替换）。打开 JSON 检查 `last_observation_date`、`trade_status`、`generated_at_utc`、`last_weekly_decision_validated`。
5. 将**完整可公开访问的 signal.json URL**发回 ChatGPT，才能把你的北京时间08:00日报接到这个稳定地址。仅生成一个链接不等于 ChatGPT 任务已连接，请等确认。

## 每日自动时间

GitHub Actions 设为每天 **UTC 23:35 = 北京时间07:35** 尝试运行。每天08:00日报可使用刚生成的数据。GitHub Actions `schedule` 非严格准时服务：可能延后或因公共平台限制未触发。因此日报**必须检查** `generated_at_utc`、`last_observation_date` 与 `trade_status`，过期时不给新的调仓指令。

## 数据源与策略

- 主数据源：FRED `NASDAQ100`。备用：yfinance `^NDX`、AKShare `.NDX`。
- 200日SMA、40日收益标准差×√252、±1%趋势迟滞、周度目标仓位、3个百分点调仓门槛，按 `scripts/ndx_signal.py` 计算。
- JSON 中 `most_recent_weekly_decision` 才是最近确认的周度信号；周中 `preview_target_pct` 不触发交易。
- 研究结果不等于交易授权。场内ETF溢价、场外申购额度必须独立检查。
- 如果某日网络/数据源全部失败，仍会发布标记为 `DATA_UNAVAILABLE_NO_ACTION` 的 JSON，避免用老数据伪造今日信号。

## 安全与限制

- 公开仓库和 Pages 会公开指标JSON，**不要把任何账号信息、授权令牌或个人资产信息提交到仓库**。
- 第三方接口可能限流、改版；失败属于预期场景，需要检查 Actions 日志并更新依赖。
- 当前使用原F-V1.1脚本的时效阈值，无法保证每个节假日都准确识别最新交易日；在日报执行前还要检查最后一个已完成美股交易日。
- 这是自动化部署包，**还未在你的GitHub账号部署或进行真实云端连续运行测试**。
