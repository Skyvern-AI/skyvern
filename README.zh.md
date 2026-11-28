<!-- DOCTOC SKIP -->

<h1 align="center">
 <a href="https://www.skyvern.com">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern_logo.png"/>
    <img height="120" src="fern/images/skyvern_logo_blackbg.png"/>
  </picture>
 </a>
 <br />
</h1>
<p align="center">
🐉 基于大语言模型（LLM）与计算机视觉自动化浏览器工作流 🐉
</p>
<p align="center">
  <a href="https://www.skyvern.com/"><img src="https://img.shields.io/badge/Website-blue?logo=googlechrome&logoColor=black"/></a>
  <a href="https://www.skyvern.com/docs/"><img src="https://img.shields.io/badge/Docs-yellow?logo=gitbook&logoColor=black"/></a>
  <a href="https://discord.gg/fG2XXEuQX3"><img src="https://img.shields.io/discord/1212486326352617534?logo=discord&label=discord"/></a>
  <!-- <a href="https://pepy.tech/project/skyvern" target="_blank"><img src="https://static.pepy.tech/badge/skyvern" alt="Total Downloads"/></a> -->
  <a href="https://github.com/skyvern-ai/skyvern"><img src="https://img.shields.io/github/stars/skyvern-ai/skyvern" /></a>
  <a href="https://github.com/Skyvern-AI/skyvern/blob/main/LICENSE"><img src="https://img.shields.io/github/license/skyvern-ai/skyvern"/></a>
  <a href="https://twitter.com/skyvernai"><img src="https://img.shields.io/twitter/follow/skyvernai?style=social"/></a>
  <a href="https://www.linkedin.com/company/95726232"><img src="https://img.shields.io/badge/Follow%20 on%20LinkedIn-8A2BE2?logo=linkedin"/></a>
</p>
<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

[Skyvern](https://www.skyvern.com) 利用大语言模型（LLM）和计算机视觉技术自动化基于浏览器的工作流。它不仅提供了一个兼容 Playwright 的 SDK（在 Playwright 之上叠加了 AI 能力），还配备了无代码工作流构建器（No-Code Workflow Builder），帮助技术人员与非技术人员在任意网站上自动化处理原本需要人工介入的业务流程，彻底取代脆弱且不可靠的传统自动化脚本。

<p align="center">
  <img src="fern/images/geico_shu_recording_cropped.gif"/>
</p>

传统的浏览器自动化方案通常需要针对特定网站编写定制脚本，并高度依赖 DOM 解析和基于 XPath 的元素交互——一旦网站页面布局发生变动，脚本就会立即失效。

Skyvern 不再单纯依赖代码写死的 XPath 交互，而是借助视觉大语言模型（Vision LLMs）实时理解网页并与之交互。

# 工作原理
Skyvern 的设计灵感源自 [BabyAGI](https://github.com/yoheinakajima/babyagi) 和 [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) 所推广的任务驱动型（Task-Driven）自主智能体架构——并在此基础上带来了一项核心飞跃：我们赋予了 Skyvern 借助 [Playwright](https://playwright.dev/) 等浏览器自动化库直接与真实网站交互的能力。

Skyvern 通过一组协同工作的智能体集群（Swarm of Agents）来理解网页内容、规划操作步骤并执行具体动作：

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern_2_0_system_diagram.png" />
  <img src="fern/images/skyvern_2_0_system_diagram.png" />
</picture>

这一架构具备以下显著优势：

1. **零代码泛化操作未知网站**：Skyvern 能够在从未见过的网站上直接运行，因为它能自动将视觉元素映射为完成工作流所需的操作动作，无需编写任何针对特定网站的定制代码。
1. **抗页面改版与布局变化**：在页面导航过程中，系统不依赖预先写死的 XPath 或其他脆弱选择器，因此对网页布局变动具有极强的鲁棒性。
1. **一套工作流跨海量网站复用**：由于具备对交互逻辑的自主推理能力，Skyvern 可以将同一套工作流直接推广应用到成百上千个不同结构的网站上。

详细的技术报告请参阅[此处](https://www.skyvern.com/blog/skyvern-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/)。

# 演示视频 (Demo)
<!-- Redo demo -->
https://github.com/user-attachments/assets/5cab4668-e8e2-4982-8551-aab05ff73a7f

# 快速开始 (Quickstart)

## Skyvern Cloud（托管云服务）
[Skyvern Cloud](https://app.skyvern.com) 是 Skyvern 的全托管云端版本，让您无需操心底层基础设施即可直接运行 Skyvern。它支持大规模并行运行多个 Skyvern 实例，并内置了防机器人检测（Anti-Bot Detection）机制、代理网络（Proxy Network）以及验证码识别器（CAPTCHA Solvers）。

如果您希望立即体验，请访问 [app.skyvern.com](https://app.skyvern.com) 注册账号。

## 本地运行（UI + Server）

请选择您偏好的安装部署方式：

> **默认数据库说明**：`skyvern quickstart` 和 `skyvern run server` 默认使用位于 `~/.skyvern/data.db` 的 SQLite 数据库，因此通过 `pip` 安装后无需配置 Postgres 或 Docker 即可开箱即用。若需改用 Postgres，可通过 `--database-string` 传入现有数据库连接串（或不加 `--no-postgres` 参数，让 `quickstart` 自动启动专属的 Postgres 容器）。Docker Compose 方式则始终使用内置的 Postgres 服务。

### 方式 A：通过 pip 安装（推荐用于本地 Python 环境）

前置依赖要求：
- [Python 3.11、3.12 或 3.13](https://www.python.org/downloads/)

Windows 环境还需额外安装：
- [Rust](https://rustup.rs/)
- 带有 C++ 开发工具与 Windows SDK 的 VS Code

#### 1. 安装 Skyvern

```bash
pip install "skyvern[all]"
```

#### 2. 启动 Skyvern

```bash
skyvern quickstart
```

`pip` 快速启动默认使用 SQLite。若需使用本地 Postgres 容器，可直接运行 `skyvern quickstart`（除非传入 `--no-postgres`，否则会自动启动 Postgres 容器），或使用 `--database-string=postgresql+psycopg://user:pass@host:5432/dbname` 连接至已有数据库。

### 方式 B：Docker Compose 部署

如果您希望将所有组件（Postgres、API、UI）完全容器化，且不想在宿主机本地安装 Python 或 Node.js，请使用此方式：

1. 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)
2. 克隆代码仓库：
   ```bash
   git clone https://github.com/skyvern-ai/skyvern.git && cd skyvern
   ```
3. 在 `.env` 文件中配置您的 LLM 提供商（若该文件不存在，也可由 `quickstart --docker-compose` 命令根据 `.env.example` 自动创建）：
   ```bash
   cp .env.example .env  # 若尚未创建则复制模板
   # 编辑 .env 文件并填入您的 LLM API 密钥
   ```
4. 启动所有服务：
   ```bash
   docker compose up -d
   ```
5. 在浏览器中打开 http://localhost:8080

### 常见问题排查 (Troubleshooting)

**报错 `(sqlite3.OperationalError) table organizations already exists`** —— 这是 `pip install skyvern==1.0.31` 版本中的已知问题。修复方法：

```bash
rm ~/.skyvern/data.db   # 删除残留的 SQLite 数据库文件
pip install --upgrade skyvern   # 1.0.32+ 版本已包含修复
skyvern quickstart
```

如果您仍在使用 1.0.31 且暂时无法升级，可改用 `uv` 进行安装：

```bash
uv pip install skyvern
```

**执行 `pip install skyvern` 时报错 `ResolutionImpossible (litellm / fastmcp)`** —— 这是 1.0.31 版本的依赖解析冲突。请升级至 1.0.32+ 版本，或使用 `uv` 安装：`uv pip install skyvern`。

## SDK 使用指南

**Skyvern 是一个赋予 Playwright AI 浏览器自动化能力的扩展 SDK。** 它在完整保留 Playwright 原生功能的基础上，额外注入了强大的 AI 能力——您可以直接使用自然语言提示词（Prompts）与网页元素交互、提取结构化数据，并自动化执行复杂的多步工作流。

**安装选项：**
- Python SDK / 云端 API 客户端：`pip install skyvern`
- 本地服务端 + 打包版 UI：`pip install "skyvern[all]"`，然后运行 `skyvern quickstart`
- 本地服务端 + 打包版 UI（搭配 Postgres）：`pip install "skyvern[all]"`，然后运行 `skyvern quickstart --database-string=postgresql+psycopg://user:pass@host:5432/dbname`
- 仅运行打包版 UI（对接现有 API 服务）：`pip install "skyvern[ui]"`，设置环境变量 `VITE_API_BASE_URL`（若 API 需要鉴权则同时设置 `VITE_SKYVERN_API_KEY`），然后运行 `skyvern run ui`
- TypeScript SDK：`npm install @skyvern/client`

### AI 驱动的页面级命令 (AI-Powered Page Commands)

Skyvern 直接在 `page` 对象上新增了四个核心 AI 命令：

| 命令 | 功能说明 |
|---------|-------------|
| `page.act(prompt)` | 使用自然语言执行页面操作（例如："Click the login button"） |
| `page.extract(prompt, schema)` | 从当前页面提取结构化数据（支持传入可选的 JSON Schema） |
| `page.validate(prompt)` | 校验当前页面状态并返回 `bool` 值（例如："Check if user is logged in"） |
| `page.prompt(prompt, schema)` | 向大语言模型发送任意提示词（支持传入可选的响应 Schema） |

此外，`page.agent` 还提供了更高阶的工作流级命令：

| 命令 | 功能说明 |
|---------|-------------|
| `page.agent.run_task(prompt)` | 自动规划并执行复杂的多步任务 |
| `page.agent.login(credential_type, credential_id)` | 使用已保存的凭据（Skyvern、Bitwarden、1Password）完成登录认证 |
| `page.agent.download_files(prompt)` | 自动导航并下载目标文件 |
| `page.agent.run_workflow(workflow_id)` | 执行预构建的工作流（Workflow） |

### AI 增强型 Playwright 动作 (AI-Augmented Playwright Actions)

所有标准 Playwright 操作均支持传入可选的 `prompt` 参数，以实现基于 AI 的智能元素定位：

| 动作类型 | 原生 Playwright 写法 | AI 增强写法 |
|--------|------------|--------------|
| 点击 (Click) | `page.click("#btn")` | `page.click(prompt="Click login button")` |
| 填写 (Fill) | `page.fill("#email", "a@b.com")` | `page.fill(prompt="Email field", value="a@b.com")` |
| 下拉选择 (Select) | `page.select_option("#country", "US")` | `page.select_option(prompt="Country dropdown", value="US")` |
| 文件上传 (Upload) | `page.upload_file("#file", "doc.pdf")` | `page.upload_file(prompt="Upload area", files="doc.pdf")` |

**三种交互模式灵活组合：**
```python
# 1. 传统 Playwright 模式 —— 使用 CSS/XPath 选择器
await page.click("#submit-button")

# 2. 纯 AI 驱动模式 —— 使用自然语言描述
await page.click(prompt="Click the green Submit button")

# 3. AI 兜底回退模式（Fallback） —— 优先尝试选择器，若定位失败则自动回退至 AI 视觉定位
await page.click("#submit-btn", prompt="Click the Submit button")
```

### 核心 AI 命令代码示例

```python
# act —— 使用自然语言执行动作
await page.act("Click the login button and wait for the dashboard to load")

# extract —— 提取结构化数据（支持可选 JSON Schema）
result = await page.extract("Get the product name and price")
result = await page.extract(
    prompt="Extract order details",
    schema={"order_id": "string", "total": "number", "items": "array"}
)

# validate —— 校验页面状态（返回布尔值 bool）
is_logged_in = await page.validate("Check if the user is logged in")

# prompt —— 向 LLM 发送任意提示词
summary = await page.prompt("Summarize what's on this page")
```

### 快速上手代码示例

**通过 Web UI 运行：**
```bash
skyvern run all
```
在浏览器中访问 http://localhost:8080 即可通过可视化界面运行任务。如果缺少打包版 UI 组件，`skyvern run ui` 会自动提示并协助安装匹配的 UI 包。

若只需启动打包版 UI 并连接至已有的 Skyvern API 服务，请先安装 `skyvern[ui]`，并在运行 `skyvern run ui` 前设置以下环境变量：

- `VITE_API_BASE_URL`（例如 `http://localhost:8000/api/v1`）—— 将 UI 指向您的 Skyvern API 地址
- `VITE_SKYVERN_API_KEY` —— 若您的 API 启用了鉴权，请填入对应的 API 密钥
- `VITE_WSS_BASE_URL` —— WebSocket 端点地址（若未设置则自动根据 `VITE_API_BASE_URL` 推导）
- `VITE_ARTIFACT_API_BASE_URL` —— 运行产物（Artifacts）下载的基础 URL
- `VITE_BROWSER_STREAMING_MODE` —— 浏览器视口实时画面流传输模式

**Python SDK 示例：**
```python
from skyvern import Skyvern

# 本地模式运行
skyvern = Skyvern.local()

# 或者连接至 Skyvern Cloud 云端服务
skyvern = Skyvern(api_key="your-api-key")

# 启动浏览器并获取工作页面
browser = await skyvern.launch_cloud_browser()
page = await browser.get_working_page()

# 将原生 Playwright 与 AI 驱动动作无缝混用
await page.goto("https://example.com")
await page.click("#login-button")  # 原生 Playwright 点击
await page.agent.login(credential_type="skyvern", credential_id="cred_123")  # AI 自动登录
await page.click(prompt="Add first item to cart")  # AI 增强点击
await page.agent.run_task("Complete checkout with: John Snow, 12345")  # AI 自主多步任务
```

**TypeScript SDK 示例：**
```typescript
import { Skyvern } from "@skyvern/client";

const skyvern = new Skyvern({ apiKey: "your-api-key" });
const browser = await skyvern.launchCloudBrowser();
const page = await browser.getWorkingPage();

// 将原生 Playwright 与 AI 驱动动作无缝混用
await page.goto("https://example.com");
await page.click("#login-button");  // 原生 Playwright 点击
await page.agent.login("skyvern", { credentialId: "cred_123" });  // AI 自动登录
await page.click({ prompt: "Add first item to cart" });  // AI 增强点击
await page.agent.runTask("Complete checkout with: John Snow, 12345");  // AI 自主多步任务

await browser.close();
```

**极简但完整的单任务执行：**
```python
from skyvern import Skyvern

skyvern = Skyvern()
task = await skyvern.run_task(prompt="Find the top post on hackernews today")
print(task)
```

## 高阶用法 (Advanced Usage)

### 控制您自己的本地 Chrome 浏览器

让 Skyvern 直接接管并控制您现有的本地 Chrome 浏览器——完整保留您已有的全部 Cookie、登录会话状态以及浏览器扩展插件。

#### 第一步：在 Chrome 中启用远程调试 (Remote Debugging)

1. 打开 Chrome 浏览器并访问 `chrome://inspect/#remote-debugging`
2. 点击 **Enable** 启动远程调试服务
3. 页面应显示：**Server running at: 127.0.0.1:9222**

> [!TIP]
> 运行 `skyvern init browser` 命令可自动引导完成此步骤——它会自动打开 `chrome://inspect/#remote-debugging`，等待您点击启用并自动保存相关配置。

#### 第二步：连接 Skyvern

**方式 A —— 通过 Python 代码连接：**
```python
from skyvern import Skyvern

skyvern = Skyvern(
    base_url="http://localhost:8000",
    api_key="YOUR_API_KEY",
    browser_address="http://127.0.0.1:9222",
)
task = await skyvern.run_task(
    prompt="Find the top post on hackernews today",
)
```

**方式 B —— 通过 Skyvern 服务配置连接：**

在您的 `.env` 配置文件中添加以下两个变量：
```bash
BROWSER_TYPE=cdp-connect
BROWSER_REMOTE_DEBUGGING_URL=http://127.0.0.1:9222
```

重启 Skyvern 服务（`skyvern run all`），即可通过 Web UI 或代码直接操控本地浏览器运行任务。

### 将 Skyvern Cloud 连接至您的本地浏览器

让云端的 Skyvern Cloud 远程操控运行在您本机上的 Chrome 浏览器——同样完整复用您本机已有的 Cookie、登录态与扩展插件。这在自动化处理已登录站点或处于企业内网 VPN 环境下的网站时尤为实用。

```bash
# 一键启动本地 Chrome 并建立通往 Skyvern Cloud 的安全隧道
skyvern browser serve --tunnel
```

随后在任务代码中传入生成的隧道 URL：

```python
from skyvern import Skyvern

skyvern = Skyvern(api_key="your-api-key")
task = await skyvern.run_task(
    prompt="Download the latest invoice from my account",
    browser_address="https://abc123.ngrok-free.dev",
)
```

> [!WARNING]
> 通过公网隧道暴露本地浏览器时，请务必始终启用 `--api-key` 参数。若未启用鉴权，任何获知该 URL 的人都能完全控制您的浏览器。详情请参阅[安全指南文档](https://www.skyvern.com/docs/optimization/browser-tunneling#security)。

有关全部配置参数、手动建立隧道及故障排查的详细说明，请参阅[完整文档](https://www.skyvern.com/docs/optimization/browser-tunneling)。

### 获取符合固定 Schema 的一致性结构化输出
您可以通过传入 `data_extraction_schema` 参数来强制规范返回数据的结构：
```python
from skyvern import Skyvern

skyvern = Skyvern()
task = await skyvern.run_task(
    prompt="Find the top post on hackernews today",
    data_extraction_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "The title of the top post"
            },
            "url": {
                "type": "string",
                "description": "The URL of the top post"
            },
            "points": {
                "type": "integer",
                "description": "Number of points the post has received"
            }
        }
    }
)
```

### 常用调试与服务管理命令

```bash
# 单独启动 Skyvern 后端服务
skyvern run server

# 单独启动 Skyvern UI 界面
skyvern run ui

# 查看 Skyvern 服务运行状态
skyvern status

# 停止所有 Skyvern 服务
skyvern stop all

# 仅停止 Skyvern UI
skyvern stop ui

# 仅停止 Skyvern 后端服务
skyvern stop server
```

# 性能与基准评测 (Performance & Evaluation)

Skyvern 在 [WebBench 基准测试](https://webbench.ai)中取得了 **64.4%** 的业界领先（SOTA）准确率。详细的技术报告与评测分析请参阅[这篇博客](https://www.skyvern.com/blog/web-bench-a-new-way-to-compare-ai-browser-agents/)。

<p align="center">
  <img src="fern/images/performance/webbench_overall.png"/>
</p>

## 写入型（WRITE）任务性能表现（如表单填写、登录、文件下载等）

在涵盖表单填写、账号登录、文件下载等核心交互的写入型（WRITE）任务评测中，Skyvern 位居所有参评智能体榜首——此类任务恰恰是工业界 RPA（机器人流程自动化）及相关生产场景的核心需求。

<p align="center">
  <img src="fern/images/performance/webbench_write.png"/>
</p>

# Skyvern 核心功能特性

## Skyvern Tasks（单任务）
Task（任务）是 Skyvern 中最基础的构建单元。每个 Task 对应向 Skyvern 发起的一次独立请求，指导它自主在目标网站中导航并完成特定目标。

创建 Task 时需要指定 `url` 和 `prompt`，同时还可选配 `data schema`（当您希望输出结果严格遵循特定 JSON 结构时）以及 `error codes`（当您希望 Skyvern 在遇到特定异常情况时立即中止运行时）。

<p align="center">
  <img src="fern/images/skyvern_2_0_screenshot.png"/>
</p>


## Skyvern Workflows（多步工作流）
Workflow（工作流）支持将多个任务与操作节点串联组合，形成一套完整的端到端业务流程单元。

例如，如果您希望批量下载 1 月 1 日之后的所有发票，可以构建一个工作流：首先导航至发票页面，接着筛选出 1 月 1 日之后的发票，提取符合条件的所有发票列表，最后通过循环遍历逐一下载每张发票。

再比如，如果您希望自动化完成电商网站的商品采购，可以构建一个工作流：首先导航至目标商品页并将其加入购物车；随后进入购物车页面并校验商品状态；最后自动走完结账流程完成下单购买。

目前支持的工作流组件块（Blocks）包括：
1. 浏览器任务（Browser Task）
1. 浏览器动作（Browser Action）
1. 数据提取（Data Extraction）
1. 状态校验（Validation）
1. 循环遍历（For Loops）
1. 文件解析（File Parsing）
1. 发送邮件（Sending Emails）
1. 文本提示词（Text Prompts）
1. HTTP 请求块（HTTP Request Block）
1. 自定义代码块（Custom Code Block）
1. 上传文件至对象存储（Uploading Files to Block Storage）
1. （即将推出）条件分支（Conditionals）

<p align="center">
  <img src="fern/images/block_example_v2.png"/>
</p>

## 实时画面流传输 (Livestreaming)
Skyvern 支持将浏览器视口画面实时流式传输到您的本地机器上，让您能够直观地观察 Skyvern 在网页上的每一步操作。这对于调试排障、理解 Skyvern 的页面交互逻辑以及在必要时进行人工介入非常有帮助。

## 智能表单填写 (Form Filling)
Skyvern 原生具备在各类网站上自动填写复杂表单的能力。只需通过 `navigation_goal` 传入所需提交的信息，Skyvern 即可自动理解字段语义并准确填入对应表单项。

## 结构化数据提取 (Data Extraction)
Skyvern 同样支持从网页中精准提取数据。

您可以在主提示词参数中直接指定 `data_extraction_schema`（JSONC 格式），明确告知 Skyvern 需要从网页中抽取哪些字段，Skyvern 的输出结果将严格遵循您所提供的 Schema 结构。

## 自动文件下载 (File Downloading)
Skyvern 支持从网站上自动定位并下载文件。所有下载的文件都会自动上传至对象存储（若已配置），您也可以随时通过 Web UI 直接查看与获取这些文件。

## 身份认证与登录 (Authentication)
Skyvern 支持多种不同的身份认证机制，帮助您轻松自动化那些需要登录后才能访问的业务流程。如果您希望深入了解或试用，欢迎通过[电子邮件](mailto:founders@skyvern.com)或 [Discord 社区](https://discord.gg/fG2XXEuQX3)与我们联系。

<p align="center">
  <img src="fern/images/secure_password_task_example.png"/>
</p>


### 🔐 双因素认证支持 (2FA / TOTP)
Skyvern 支持多种双因素认证（2FA）方式，助力您无缝自动化要求二次验证的工作流。

支持的 2FA 方式包括：
1. 基于二维码/密钥的 TOTP 动态口令（例如 Google Authenticator、Authy）
1. 基于电子邮件的 2FA 验证码
1. 基于短信（SMS）的 2FA 验证码

🔐 了解更多关于 2FA 支持的详情，请参阅[此文档](https://www.skyvern.com/docs/credentials/totp)。

### 密码管理器集成 (Password Manager Integrations)
Skyvern 目前支持及规划中的密码管理器集成如下：
- [x] Bitwarden
- [x] 自定义凭据服务（Custom Credential Service，基于 HTTP API）
- [ ] 1Password
- [ ] LastPass


## 模型上下文协议 (Model Context Protocol, MCP)

Skyvern 原生支持模型上下文协议（MCP），允许您在任何支持 MCP 协议的大语言模型或客户端中直接调用 Skyvern。

请参阅 [MCP 集成文档](https://www.skyvern.com/docs/integrations/mcp#mcp-server)。

## Zapier / Make.com / n8n 集成
Skyvern 支持与 Zapier、Make.com 以及 n8n 集成，方便您将 Skyvern 工作流与海量第三方应用无缝串联：

* [Zapier](https://www.skyvern.com/docs/integrations/zapier)
* [Make.com](https://www.skyvern.com/docs/integrations/make.com)
* [N8N](https://www.skyvern.com/docs/integrations/n8n)

🔐 了解更多关于 2FA 支持的详情，请参阅[此文档](https://www.skyvern.com/docs/credentials/totp)。


# Skyvern 真实场景应用案例
我们非常高兴看到 Skyvern 在各行各业的真实生产环境中大显身手。以下是使用 Skyvern 自动化真实业务工作流的部分典型案例，也欢迎大家提交 PR 分享您自己的应用案例！

## 跨海量不同网站自动下载发票
[预约在线演示查看实时效果](https://meetings.hubspot.com/skyvern/demo)

<p align="center">
  <img src="fern/images/invoice_downloading.gif"/>
</p>

## 自动化求职网申流程
[💡 在线体验](https://app.skyvern.com/tasks/create/job_application)
<p align="center">
  <img src="fern/images/job_application_demo.gif"/>
</p>

## 为制造企业自动化物料寻源与采购
[💡 在线体验](https://app.skyvern.com/tasks/create/finditparts)
<p align="center">
  <img src="fern/images/finditparts_recording_crop.gif"/>
</p>

## 自动导航政务网站完成账户注册或表单申报
[💡 在线体验](https://app.skyvern.com/tasks/create/california_edd)
<p align="center">
  <img src="fern/images/edd_services.gif"/>
</p>
<!-- Add example of delaware entity lookups x2 -->

## 自动填写各类网站的“联系我们”表单
[💡 在线体验](https://app.skyvern.com/tasks/create/contact_us_forms)
<p align="center">
  <img src="fern/images/contact_forms.gif"/>
</p>


## 跨任意语言从各大保险公司网站获取保险报价
[💡 在线体验](https://app.skyvern.com/tasks/create/bci_seguros)
<p align="center">
  <img src="fern/images/bci_seguros_recording.gif"/>
</p>

[💡 在线体验](https://app.skyvern.com/tasks/create/geico)

<p align="center">
  <img src="fern/images/geico_shu_recording_cropped.gif"/>
</p>

# 贡献者本地开发环境搭建 (Contributor Setup)
请确保您已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
1. 运行以下命令创建虚拟环境（`.venv`）：
    ```bash
    uv sync --group dev
    ```
2. 执行服务端初始化配置：
    ```bash
    uv run skyvern quickstart
    ```
3. 在浏览器中访问 `http://localhost:8080` 即可开始使用 UI 界面。
   *Skyvern CLI 全面支持 Windows、WSL、macOS 和 Linux 环境。*

# 官方文档 (Documentation)

更详尽的使用文档请访问我们的 [📕 文档中心](https://www.skyvern.com/docs)。如果您发现任何表述不清或遗漏之处，欢迎随时提交 Issue，或通过[电子邮件](mailto:founders@skyvern.com)及 [Discord 社区](https://discord.gg/fG2XXEuQX3)告知我们。

# 支持的大语言模型 (Supported LLMs)
| 模型提供商 (Provider) | 支持的模型 (Supported Models) |
| -------- | ------- |
| OpenAI   | GPT-5.5, GPT-5.4, GPT-5, GPT-4.1, o3, o4-mini |
| Anthropic | Claude 4.7 Opus, Claude 4.6 (Sonnet, Opus), Claude 4.5 (Haiku, Sonnet, Opus) |
| Azure OpenAI | 部署在您的 Azure 订阅下的任意 GPT 模型 |
| AWS Bedrock | Claude 4.7, Claude 4.6 (Sonnet, Opus), Claude 4.5 (Sonnet, Opus) |
| Gemini | Gemini 3.1 Pro, Gemini 3 Flash, Gemini 2.5 Pro/Flash |
| Ollama | 通过 [Ollama](https://github.com/ollama/ollama) 运行任意本地托管模型 |
| OpenRouter | 通过 [OpenRouter](https://openrouter.ai) 接入各类主流模型 |
| OpenAI 兼容接口 | 任何遵循 OpenAI API 格式的自定义端点（基于 [LiteLLM](https://docs.litellm.ai/docs/providers/openai_compatible)） |

有关包括所有可用模型 Key、环境变量及多模型组合配置在内的详细说明，请参阅 [LLM 配置文档](https://www.skyvern.com/docs/self-hosted/llm-configuration)。

# 参与贡献 (Contributing)

我们非常欢迎各类 PR 与改进建议！请随时提交 PR / Issue，或通过[电子邮件](mailto:founders@skyvern.com)及 [Discord 社区](https://discord.gg/fG2XXEuQX3)与我们交流。
建议先阅读我们的[贡献指南 (CONTRIBUTING.md)](CONTRIBUTING.md) 并查看带有 ["Help Wanted" 标签的 Issues](https://github.com/skyvern-ai/skyvern/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22) 快速上手！

如果您希望通过对话方式快速了解 Skyvern 代码库的整体架构、扩展开发方式或解答使用疑问，欢迎体验 [Code Sage](https://sage.storia.ai?utm_source=github&utm_medium=referral&utm_campaign=skyvern-readme)。

# 遥测说明 (Telemetry)

默认情况下，Skyvern 会收集基础的匿名使用统计信息，以帮助我们了解 Skyvern 的使用情况并持续改进产品。如果您希望关闭遥测，只需将环境变量 `SKYVERN_TELEMETRY` 设置为 `false` 即可。

# 开源许可证 (License)
Skyvern 的开源代码仓库由托管云服务提供商业支持。驱动 Skyvern 的全部核心逻辑均在本开源仓库中基于 [AGPL-3.0 许可证](LICENSE)开放提供（仅托管云服务内置的高级反爬虫对抗策略除外）。

如果您对许可证授权有任何疑问或顾虑，请随时[联系我们](mailto:support@skyvern.com)，我们将竭诚为您解答。

# Star 增长趋势 (Star History)

[![Star History Chart](https://api.star-history.com/svg?repos=Skyvern-AI/skyvern&type=Date)](https://star-history.com/#Skyvern-AI/skyvern&Date)

---

> _注：本简体中文文档由社区志愿者翻译维护。若与英文原版存在差异，请以 [README.md](README.md) 为准。_
