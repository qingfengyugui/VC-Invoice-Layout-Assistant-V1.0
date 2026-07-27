# Invoice Layout Agent

把 PDF、图片、OFD、XML、ZIP/7z/TAR/RAR 或混合文件整理为可直接打印的 A4 报销附件，全程保留原始财务凭证内容，不生成、不补写、不美化票面。

每次处理固定输出：

- `sendable.pdf`：可寄送/提交的票据页，无页码、类目、文件名、注释、边框或裁切线。
- `printable.pdf`：相同票据页，加一张独占最后一页的检查提醒；提醒页不与票据页共享，也不应寄送。
- `report.json`：私密核查报告，不进入 Git，不随报销材料发送。

默认顺序为机票、铁路、住宿、打车、其他交通。引擎按票据尺寸和可读性选择单页密度，一张票据不会跨页。实体票照片不会进入电子票据页，只在末页提醒另附原件。

## 一键安装

普通使用者不需要预装 Python、Java、Maven、Poppler、WPS、Docker、OCR 或解压工具。Release 运行包已经包含 Python 应用、PDFium、精简 Java/OFDRW 和 7-Zip；安装器会校验 `SHA256SUMS`，安装对应平台运行包和 Skill，并在检测到 Codex、Claude Code 或 Qoder CLI 时注册本地 MCP。

Windows PowerShell：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/qingfengyugui/invoice-layout-agent/main/install.ps1 -OutFile install.ps1
.\install.ps1 -Platform codex
```

macOS / Linux：

```bash
curl -fsSLO https://raw.githubusercontent.com/qingfengyugui/invoice-layout-agent/main/install.sh
sh install.sh --platform codex
```

把 `codex` 换成 `claude-code`、`openclaw`、`workbuddy`、`qoder` 或 `qclaw` 即可。默认支持 Windows x64、Linux x64、macOS Apple Silicon 和 macOS Intel；可用 `--destination` 指定非默认 Skill 目录，升级现有安装时追加 `--force`。

Skill 目录里的 `RUNTIME.md` 记录完整运行包的绝对路径，Agent 不依赖全局 PATH。`doctor` 会验证包内 PDFium、Java/OFDRW、RAR 解压器、中文字体和临时目录。OFD 不经过 WPS；RAR 缺失或解压失败会明确终止，不会静默漏票。

宿主平台使用自身多模态能力查看本机生成的预览，再把带页码与哈希绑定的观察结果交回确定性引擎，不要求用户另配模型 API 密钥。本地 OCR 只是非图片输入的可选降级路径；图片票据或实体票照片必须走宿主多模态观察流程，本地模式无法可靠区分时会终止且不产生半成品。

## 直接处理

先检查运行环境：

```bash
invoice-layout doctor
```

让 Agent 使用宿主多模态能力时，先生成严格绑定的观察请求：

```bash
invoice-layout prepare sample-input --request private-work/request.json --work-dir private-work
```

宿主检查全部预览并生成观察清单后：

```bash
invoice-layout process sample-input --observations private-work/observations.json --provider host --work-dir private-work --output-dir final-output
```

不使用宿主多模态且输入不含图片源文件时，可明确选择本地 OCR：

```bash
invoice-layout process sample-input --provider local --work-dir private-work --output-dir final-output
```

输入可以是一个或多个文件、文件夹、图片或归档。图片输入必须使用上面的 `prepare` + `--provider host` 流程，以避免实体票照片被错误排入电子粘贴页。源文件、预览、观察清单和输出都应放在私密工作目录，不要提交到版本库。

## 开发者安装与 Docker

只有参与源码开发时才需要 Python 3.11–3.13、Java 17 和 Maven：

```bash
python -m pip install -e ".[dev]"
python scripts/install_skill.py --platform codex --destination .agents/skills
```

Docker 只是开发和持续集成的替代运行方式，不是普通用户前置条件。容器内包含完整运行环境：

```bash
docker build -t invoice-layout-agent .
docker run --rm -v "./sample-input:/input:ro" -v "./final-output:/output" invoice-layout-agent process /input --provider local --work-dir /tmp/private-work --output-dir /output
```

上面的容器直跑命令适用于不含图片源文件的输入；图片输入同样需要宿主多模态观察清单。

## 安全边界

- 普通 A4 页只包含原始票据内容或安全裁切，不叠加说明文字。
- 不使用图像生成，不修复、增强、锐化、重绘或填写财务凭证。
- 不猜测金额、日期、发票号或关联关系；不确定项只进入私密报告和末页提醒。
- 自动像素比对只能由逐页人工目检处理确定性渲染差异，不能放行缺页、遮挡、重叠、错误页数或非 A4 页面。
- 示例和测试只使用合成几何文件；真实票据、路径、报告和模型响应不得进入 Git。

## 开发验证

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m mypy src
python -m pytest -q --cov=invoice_layout --cov-fail-under=85
python scripts/validate_adapters.py
```

许可证：[Apache License 2.0](LICENSE)。
