# GDUST 课表抓取工具 🎓

专为广东科技学院学生设计的课表数据抓取工具。通过学校 WebVPN 代理访问教务门户 API，自动获取每学期 20 周课表数据，输出为 JSON 文件。

## 它能做什么

- 输入学号 + 校园账号密码，自动登录 CAS 统一认证
- 通过 WebVPN 代理请求教务系统课表接口
- 支持 **ddddocr 自动识别验证码**（全自动），也支持手动输入
- 按周抓取（第 1~20 周），增量合并到本地 JSON 文件
- **Web GUI**：浏览器操作，可视化界面，表格展示课表

## 前置条件

- Python 3.9+
- 广东科技学院校园网账号（学号 + 密码）
- 网络环境：校园网 或 已连接学校 VPN

## 项目结构

```
gdust-timetable/
├── start.sh              # 一键启动（macOS/Linux）
├── start.bat             # 一键启动（Windows）
├── gdust_timetable.py    # 核心逻辑（命令行 & Web 共用）
├── app.py                # Web GUI（Flask）
├── templates/
│   └── index.html        # Web 界面
├── config.example.json   # 配置模板
├── requirements.txt      # Python 依赖
├── README.md
├── LICENSE
└── .gitignore

运行后会额外生成：
├── config.json           # 你的配置（已 gitignore）
├── timetable.json        # 抓取到的课表数据
├── captcha.jpg           # 验证码图片（临时）
└── ~/.config/gdust-timetable/secrets.json  # 账号密码 & TOKEN（已 gitignore）
```

## 安装

```bash
git clone https://github.com/你的用户名/gdust-timetable.git
cd gdust-timetable
pip3 install -r requirements.txt
```

`ddddocr` 是可选的，不装也能用（手动输验证码）：
```bash
pip3 install ddddocr  # 可选：自动验证码识别
```

## 使用方法

### 方式一：Web GUI（推荐）

**macOS / Linux：**
```bash
./start.sh
```

**Windows：**
```
双击 start.bat
```

一键启动：自动检查依赖（默认走清华镜像源）、首次运行自动复制配置模板、打开浏览器即可用。

也可以手动启动：
```bash
python3 app.py
```

浏览器打开 `http://localhost:5000`，界面操作：

1. 填写学号等配置信息，点「保存配置」
2. 输入校园账号密码，点「保存账号密码」
3. 选择登录方式（自动/手动验证码/粘贴 TOKEN）
4. 点「抓取当前周」或「抓取全学期」
5. 表格查看课表，点「下载 JSON」导出

### 方式二：命令行

### 方式二：命令行

#### 第一步：配置学号

```bash
cp config.example.json config.json
```

编辑 `config.json`，填写你的信息：

```json
{
  "job_number": "202xxxxxxxxxx",   ← 你的学号
  "week1_monday": "2026-03-09",    ← 本学期第一周的周一日期
  "network_mode": "campus"         ← campus=校内直连，webvpn=校外代理
}
```

> 💡 `week1_monday` 用来计算当前是第几教学周。开学后周一的日期就是这个值。
> 💡 `network_mode`：连接校园网时填 `campus`，校外填 `webvpn`。Web 界面中也可切换。

#### 第二步：保存校园账号

```bash
python3 gdust_timetable.py --set-credentials
```

按提示输入学号和密码。密码存储在 `~/.config/gdust-timetable/secrets.json`，权限 0600（仅自己可读）。

#### 第三步：抓取课表

有三种登录方式，任选其一：

#### 方式一：全自动登录（推荐）

需要安装 `ddddocr`，脚本会自动识别验证码，失败自动重试 3 次：

```bash
python3 gdust_timetable.py --auto-login --all-weeks
```

#### 方式二：交互式登录

不装 ddddocr 也能用，脚本会弹出验证码图片，你手动输入：

```bash
python3 gdust_timetable.py --refresh-login --all-weeks
```

#### 方式三：从浏览器抓 TOKEN

1. 用浏览器登录 [教务门户](https://portal.gdust.edu.cn)
2. 打开 DevTools → Network → 找任意一个带 `TOKEN` header 的请求
3. 复制 TOKEN 值
4. 粘贴到命令行：

```bash
python3 gdust_timetable.py --set-token "粘贴的TOKEN"
python3 gdust_timetable.py --all-weeks
```

### 抓取单周

不加 `--all-weeks` 默认抓当前教学周：

```bash
python3 gdust_timetable.py              # 当前周
python3 gdust_timetable.py --week 5     # 第 5 周
```

### 指定输出路径

```bash
python3 gdust_timetable.py -o ~/Desktop/my_timetable.json --all-weeks
```

## 输出格式

输出文件默认为 `timetable.json`，结构如下：

```json
{
  "1": [
    {
      "courseName": "高等数学 A",
      "courseDate": "2026-03-09",
      "whichSection": 1,
      "classroomName": "松山湖校区 A301",
      "teacherName": "张三"
    },
    {
      "courseName": "大学英语",
      "courseDate": "2026-03-11",
      "whichSection": 3,
      "classroomName": "松山湖校区 B205",
      "teacherName": "李四"
    }
  ],
  "2": [...]
}
```

- **键**：教学周编号（字符串，"1" ~ "20"）
- **值**：该周的课程数组
- **字段名**：以教务系统 API 实际返回为准（`courseName`、`kcmc`、`name` 等可能因接口版本不同）

## 命令速查

| 命令 | 说明 |
|------|------|
| `--set-credentials` | 设置校园账号密码（首次使用） |
| `--set-token TOKEN` | 手动设置 portal TOKEN |
| `--set-vpn-cookies JSON` | 手动设置 VPN cookies（JSON 字符串） |
| `--auto-login` | 全自动登录（需 ddddocr） |
| `--refresh-login` | 交互式登录（手动输验证码） |
| `--begin-captcha-login` | 获取验证码图片（半自动流程第一步） |
| `--submit-captcha CODE` | 提交验证码（半自动流程第二步） |
| `--week N` | 抓取第 N 周（1-20） |
| `--all-weeks` | 抓取全学期 20 周 |
| `-o PATH` | 指定输出文件路径（默认 `timetable.json`） |

## 工作原理

本工具支持两种网络模式：

### 校内直连模式（campus）

在校园网内直接访问教务门户 API，无需经过 WebVPN 代理：

```
┌──────────────┐     ┌────────────────────────────┐
│  CAS 认证    │ ──→ │  portal.gdust.edu.cn       │
│  (账号+密码  │     │  /smart-admin-api/...       │
│   +验证码)   │     │  (直接请求)                 │
└──────────────┘     └────────────┬───────────────┘
                                  │
                                  ▼
                       ┌──────────────────┐
                       │  输出 JSON       │
                       └──────────────────┘
```

### 校外 WebVPN 模式（webvpn）

通过学校 WebVPN 代理访问教务系统，适合校外网络：

```
┌──────────────┐     ┌───────────────┐     ┌────────────────────────────┐
│  CAS 认证    │ ──→ │  获取 TGC     │ ──→ │  WebVPN 代理                │
│  (账号+密码  │     │  (登录凭证)   │     │  webvpn.gdust.edu.cn/...    │
│   +验证码)   │     │               │     │  → portal.gdust.edu.cn      │
└──────────────┘     └───────────────┘     └────────────┬───────────────┘
                                                        │
                                                        ▼
                                             ┌──────────────────┐
                                             │  输出 JSON       │
                                             └──────────────────┘
```

## TOKEN 过期怎么办

TOKEN 有有效期，过期后脚本会提示。刷新方式按方便程度排序：

1. `--auto-login` — 自动 OCR 验证码，最省事
2. `--refresh-login` — 手动输入验证码，不依赖额外包
3. `--set-token` — 从浏览器 DevTools 抓，最可靠

## 安全说明

- 校园账号密码存储在 `~/.config/gdust-timetable/secrets.json`，文件权限 0600
- `config.json` 和 `secrets.json` 已在 `.gitignore` 中排除，不会被提交
- **请勿将学号、密码、TOKEN 提交到公开仓库**

## 相关项目

- [GDUST_timetable_lite](https://github.com/chenzhuoxi/GDUST_timetable_lite) — Flutter 课表查看 App（纯展示，导入 JSON 即用，支持文件导入）

## 关于

本工具专为广东科技学院（GDUST）开发，依赖学校特定的 WebVPN 代理、CAS 认证和教务门户 API。

## License

MIT
