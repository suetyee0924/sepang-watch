# sepang-watch

监测 2026 F1 雪邦站（Gulf Air Bahrain Grand Prix in Malaysia，10 月 2–4 日）的
开票状态、Paddock Club 款待发售，以及梅奔 / Petronas 在吉隆坡的市区活动预告。

**不做**自动下单、不绕验证码。职责只有一个：**开卖那一刻把你叫醒。**

---

## 快速开始

```bash
git clone <your-repo> && cd sepang-watch
pip install -r requirements.txt

# 1) 先空跑一轮，看哪些站点能抓、哪些被 Cloudflare 拦
python watch.py --dry-run

# 2) 建立初始快照（这一轮不告警）
python watch.py --init

# 3) 之后每次运行都会跟上一次比对
python watch.py
```

## 告警语义（重要）

脚本默认是**安静的**：没有真变化就一条推送都不发。三种情况才会叫你——

| 信号 | 含义 | 优先级 |
|---|---|---|
| 关键词**新出现** | 上一轮快照里没有、这一轮冒出来了（如 `buy now`） | 🔴 urgent |
| `negative_keywords` **消失** | 「即将开卖」这类字样没了 —— 最强的开卖信号 | 🔴 urgent |
| 内容变了但关键词没动 | 页面改版 / 文案微调 | 🟡 / 🟠 |

关键是**「新出现」而不是「存在」**。`bahrain`、`malaysia`、`f1`、`october` 这类词
常年挂在这些页面上，如果每轮都判「在不在」，59 个目标里有一半会永远报红，
真正开卖那一下反而淹在噪音里。所以只比对「上一轮没有、这一轮有」。

**抓取失败不推送。** 反爬、限流、站点抽风是常态，只会打进日志，不会半夜叫醒你。

> 副作用：某个目标如果在建基线时正好被 403 挡住，之后恢复正常的那一轮，
> 它页面上所有关键词都算「新出现」，会误报一次。见到一次就当它复活了。

## 配推送通道（四选一或多选）

脚本会把配置了的通道全部推一遍。**推荐用 ntfy —— 不需要注册任何账号、不需要手机号。**

### 方案 A：ntfy.sh（首选，零注册）

1. 手机装 **ntfy** App（[iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / F-Droid），打开即用，不需要注册
2. 生成一个别人猜不到的话题名：

   ```bash
   python -c "import secrets; print('sepang-' + secrets.token_urlsafe(16))"
   ```

3. App 里点 + 号，**Subscribe to topic**，填入刚生成的字符串
4. 本地测试：

   ```bash
   export NTFY_TOPIC="你刚生成的话题名"
   python watch.py --dry-run
   ```

5. GitHub 仓库加 secret：`NTFY_TOPIC`

> ⚠️ **话题名就是密码**。ntfy.sh 是公共服务，任何知道话题名的人都能收到你的推送、也能往里发消息。所以一定要用上面命令生成的随机串，别用 `sepang-watch` 这种能猜到的名字。推送内容本身只有公开网页的变化，泄露风险不大，但没必要让人看。

### 方案 B：GitHub Issue（零配置，天然备份）

不用任何第三方服务。脚本发现变化时在你自己的仓库开一个 Issue，GitHub 会自动给你发**邮件 + 手机 App 推送**（装 GitHub 移动端即可）。

- **默认关闭**。要开就把仓库变量 `USE_GITHUB_ISSUE` 设成 `true`：

  ```bash
  gh variable set USE_GITHUB_ISSUE --body true
  ```

  （`GITHUB_TOKEN` 和 `GITHUB_REPOSITORY` 在 Actions 里永远有值，只靠它们判断
  会导致每次告警都顺手开一个 Issue —— 所以必须有这个开关。）
- 只需确保 workflow 有 `issues: write` 权限（已写好）
- 好处：告警自带完整历史记录，可以回头翻；坏处：比 ntfy 慢几分钟，且 Issue 会堆积

建议 **A + B 同时开**：ntfy 负责快，Issue 负责留档和兜底。

### 方案 C：Discord Webhook

1. 建一个自己的服务器（左栏 + → 亲自创建 → 仅供自己使用），加一个频道比如 `#sepang-alerts`
2. 频道右侧齿轮 **编辑频道** → **整合 Integrations** → **创建 Webhook** → **复制 Webhook 网址**
3. 存成 GitHub secret：`DISCORD_WEBHOOK_URL`
4. 本地测试：

   ```bash
   export DISCORD_WEBHOOK_URL="你复制的URL"
   python watch.py --dry-run --only zk-product
   ```

**关键一步 —— 让手机真的能弹出提醒**：Webhook 发的消息默认不会触发推送，频道被静音时更收不到。解决办法是让脚本在高优先级告警时 `@` 你自己：

- Discord 设置 → 高级 → 打开**开发者模式**
- 右键点自己的头像 → **复制用户 ID**
- 存成 GitHub secret：`DISCORD_MENTION_ID`

配好后，关键词命中或「即将开卖」字样消失时，消息会带上 `@你` 并加一行 ⚠️ 开卖信号，手机必弹。普通变化则安静地发到频道里，不打扰你。

再去手机端把这个频道的通知设成「所有消息」，服务器别静音。

### 方案 D：Slack Webhook

有 Slack workspace 的话，加个 Incoming Webhook，URL 存成 `SLACK_WEBHOOK_URL`。

### 方案 E：Telegram

如果能注册的话仍然可用，配 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` 两个 secret 即可（步骤见 BotFather）。

## 上云（推荐）

推到 GitHub 后，`.github/workflows/watch.yml` 会每 20 分钟自动跑，
快照写回 `state/`。先手动触发一次 workflow_dispatch 并勾选 `init` 建立基线。

> GitHub 免费额度的 cron 有 5–20 分钟延迟，别当成硬保证。
> 开票当天（一旦官方预告了具体时间）请人工同时盯着。

---

## 交给 Claude Code 做的事

这套脚本的弱点是**爬 HTML 不稳**——票务站普遍是 SPA + Cloudflare。
把下面几件事丢给 Claude Code，它比手写快：

1. **找真实接口**
   > "帮我打开 tickets.formula1.com，抓开发者工具 Network 面板里返回票种和库存的
   > JSON 接口，把它加进 targets.yaml，mode 设成 json"

   直接打接口比爬 HTML 稳一个数量级，也不容易被反爬拦。

2. **处理被 403 的目标**
   > "targets.yaml 里 bahraingp-tickets 返回 403，帮我改成用 Playwright
   > 无头浏览器抓，只在这一个目标上用"

3. **加 RSS 目标**
   > "把 Paul Tan 的 RSS（https://paultan.org/feed/）加进来，
   > 只在标题或摘要里出现 sepang/f1/ticket 时才告警"

4. **开票后切换模式**
   开卖那天，`seating-probe` 这个目标要指向真实选座页，用来判断
   **分区对号入座 vs 自由入座**——这直接决定你是抢座位号还是抢分区。

5. **批量补齐代理网址**

   `targets.yaml` 里只放了已核实域名的几家代理。剩下 20 多家可以让 agent 一次性抓出来：

   > "打开 https://f1experiences.com/authorized-sales-agents ，把页面上所有代理的
   > 公司名、所在国家、官网链接提取成一张表。然后只把亚太地区（日本、新加坡、
   > 香港、澳洲、新西兰、泰国、印度）的代理追加成 targets.yaml 的新目标，
   > keywords 统一用 malaysia / sepang / bahrain / october"

   为什么只加亚太：你在东京，同时区代理沟通成本最低，且欧美代理的库存通常
   不面向亚太客户。全加进去只会稀释告警信号。

6. **定期体检**
   > "跑一遍 watch.py --dry-run，把所有抓取失败的目标列出来并修好"

---

## Google Alerts 接入

去 https://www.google.com/alerts 建这几条，**投递方式选 RSS**，
把生成的 feed 地址填进 `targets.yaml`（`mode: text`）：

| 关键词 | 抓什么 |
|---|---|
| `"Sepang" F1 tickets` | 开票新闻 |
| `"Bahrain Grand Prix" Malaysia tickets` | 官方口径变化 |
| `Sepang "Paddock Club"` | 款待发售 |
| `Petronas KLCC Mercedes` | 市区车迷活动 |
| `雪邦 F1 门票` | 中文圈消息 |

## 社媒怎么办

X / Instagram 的爬取成本远高于回报（API 收费、反爬严格）。
**手机上直接开推送通知**是最优解，覆盖这三个账号：

- `@PETRONAS` / `@petronasofficial`
- `@MercedesAMGF1`
- Suria KLCC 官方 IG / FB

历史经验：这次的票价泄露最早出现在 X 用户和本地科技媒体，
**不是**官方账号——所以本仓库监测的 Paul Tan / Lowyat / SoyaCincau
其实比官方社媒更早。

---

## 开票当天的动作清单

1. 收到 🔴 告警 → 立刻打开页面，**先看有没有选座**
   - 有座位图 → 抢正对 3–4 号 pit box 的分区（梅奔车库，2025 车队榜第二）
   - 无座位图 → 说明自由入座，买能进主看台的最低档即可，位置靠现场早到
2. 款待票**不要**直接付第三方报价，先比对 F1 Experiences 官方价
3. 任何声称"官方授权"的代理，拿 `f1exp-agents` 这个目标抓下来的
   名单核对——中国大陆目前**没有**任何一家在官方授权名单上

---

## 目录

```
watch.py                     主脚本
targets.yaml                 监测目标（改这个文件就够了）
requirements.txt
state/snapshots.json         快照（自动生成，勿手改）
.github/workflows/watch.yml  定时任务
```
